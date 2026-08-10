"""B 站真实互动动作：点赞 / 关注 / 转发 / 评论（对齐 bilibinggo lottery_actions）

参与抽奖需完成互动动作（三连：点赞+转发+评论，通常还需关注）：
  like    -> x/dynamic/feed/dyn/thumb      (POST + csrf)
  follow  -> x/relation/modify             (POST form + csrf)
  repost  -> dynamic_repost/v1/dynamic_repost/repost (POST form + csrf)
  comment -> x/v2/reply/add                (POST form + csrf)

说明：
- 这些接口都需要登录态（session cookies）与 csrf（cookie 里的 bili_jct）
- 调用前需用账号 cookies 构建 BiliClient（self.session 已带 cookie）
- 幂等：已点赞/已转发/已评论时接口返回特定 code，视为成功跳过
"""
import time

from . import bili_client
from .rate_limit import acquire_bili_request_slot

LIKE_URL = "https://api.bilibili.com/x/dynamic/feed/dyn/thumb"
FOLLOW_URL = "https://api.bilibili.com/x/relation/modify"
REPOST_URL = "https://api.vc.bilibili.com/dynamic_repost/v1/dynamic_repost/repost"
COMMENT_URL = "https://api.bilibili.com/x/v2/reply/add"
REPLY_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"
RELATION_URL = "https://api.bilibili.com/x/relation"

ACTION_INTERVAL_MIN = 1.5   # 动作间隔下限（防风控，随机化避免规律被检测）
ACTION_INTERVAL_MAX = 3.0   # 动作间隔上限


def _action_gap() -> float:
    """动作之间随机间隔（1.5~3.0s 抖动，模拟真人操作节奏）"""
    import random as _random
    # 优先读设置项（防风控可调），默认 1.5~3.0
    lo, hi = ACTION_INTERVAL_MIN, ACTION_INTERVAL_MAX
    try:
        from ..database import SessionLocal
        from .. import models as _models
        db = SessionLocal()
        try:
            rows = {r.key: r.value for r in db.query(_models.Setting).all()}
            lo = max(0.3, float(rows.get("action_interval_min", ACTION_INTERVAL_MIN)))
            hi = max(lo, float(rows.get("action_interval_max", ACTION_INTERVAL_MAX)))
        finally:
            db.close()
    except Exception:
        pass
    return _random.uniform(lo, hi)

ACTION_LABELS = {
    "like": "点赞", "follow": "关注", "repost": "转发", "comment": "评论",
}

# 幂等返回码（已做过该动作）
_IDEMPOTENT_CODES = {
    "like": {65006},      # 已赞过
    "follow": {22014},    # 已关注
    "repost": set(),      # 文案含"已"/"重复"
    "comment": {12051},   # 已有相同评论
}


def get_csrf(client: bili_client.BiliClient) -> str:
    """从 session cookies 取 csrf（bili_jct），缺失返回空串"""
    return client.session.cookies.get("bili_jct", "") or ""


def get_my_uid(client: bili_client.BiliClient) -> str:
    """当前登录用户 uid（从 cookies DedeUserID）"""
    return client.session.cookies.get("DedeUserID", "") or ""


def _api_code(payload: dict) -> int:
    return int(payload.get("code") or 0)


def _api_message(payload: dict) -> str:
    return str(payload.get("message") or "")


def extract_comment_oid(detail: dict) -> tuple[str, int]:
    """从动态详情提取评论所需的 oid(comment_id_str) 与 comment_type"""
    basic = detail.get("basic") or {}
    if not isinstance(basic, dict):
        basic = {}
    rid = str(basic.get("comment_id_str") or detail.get("id_str") or "")
    ctype = int(basic.get("comment_type") or 17)
    return rid, ctype


def like_dynamic(client: bili_client.BiliClient, *, dynamic_id: str,
                 csrf: str, referer: str) -> dict:
    """点赞动态，返回 {ok, message}"""
    try:
        acquire_bili_request_slot()
        r = client.session.post(
            LIKE_URL,
            json={"dyn_id_str": dynamic_id, "up": 1,
                  "spmid": "333.1369.0.0", "from_spmid": "333.999.0.0"},
            params={"csrf": csrf},
            headers={"Referer": referer},
            timeout=12)
        p = r.json()
        code = _api_code(p)
        if code == 0:
            return {"ok": True, "action": "like", "message": "点赞成功"}
        if code == 65006:
            return {"ok": True, "action": "like", "message": "已赞过"}
        return {"ok": False, "action": "like",
                "message": f"点赞失败 code={code} {_api_message(p)}".strip()}
    except Exception as e:
        return {"ok": False, "action": "like", "message": f"点赞异常: {e}"}


def follow_user(client: bili_client.BiliClient, *, uid: str,
                csrf: str, referer: str) -> dict:
    """关注 UP 主，返回 {ok, message}"""
    try:
        acquire_bili_request_slot()
        r = client.session.post(
            FOLLOW_URL,
            data={"fid": uid, "act": 1, "re_src": 11, "csrf": csrf},
            headers={"Referer": referer},
            timeout=12)
        p = r.json()
        code = _api_code(p)
        if code == 0:
            return {"ok": True, "action": "follow", "message": f"关注成功 uid={uid}"}
        if code == 22014:
            return {"ok": True, "action": "follow", "message": f"已关注 uid={uid}"}
        return {"ok": False, "action": "follow",
                "message": f"关注失败 code={code} {_api_message(p)}".strip()}
    except Exception as e:
        return {"ok": False, "action": "follow", "message": f"关注异常: {e}"}


def repost_dynamic(client: bili_client.BiliClient, *, dynamic_id: str,
                   my_uid: str, csrf: str, referer: str, content: str) -> dict:
    """转发动态（带文案），返回 {ok, message}"""
    try:
        acquire_bili_request_slot()
        r = client.session.post(
            REPOST_URL,
            data={"uid": str(my_uid), "dynamic_id": dynamic_id,
                  "content": (content or "")[:233], "ctrl": "[]", "csrf": csrf},
            headers={"Referer": referer},
            timeout=15)
        p = r.json()
        code = _api_code(p)
        if code == 0:
            return {"ok": True, "action": "repost", "message": f"转发成功：{(content or '')[:40]}"}
        msg = _api_message(p)
        if "已" in msg or "重复" in msg:
            return {"ok": True, "action": "repost", "message": "已转发"}
        return {"ok": False, "action": "repost",
                "message": f"转发失败 code={code} {msg}".strip()}
    except Exception as e:
        return {"ok": False, "action": "repost", "message": f"转发异常: {e}"}


def comment_dynamic(client: bili_client.BiliClient, *, rid: str,
                    comment_type: int, message: str, csrf: str,
                    referer: str) -> dict:
    """评论动态，返回 {ok, message}"""
    try:
        acquire_bili_request_slot()
        r = client.session.post(
            COMMENT_URL,
            data={"oid": rid, "type": comment_type,
                  "message": (message or "")[:200], "csrf": csrf},
            headers={"Referer": referer},
            timeout=12)
        p = r.json()
        code = _api_code(p)
        if code == 0:
            return {"ok": True, "action": "comment", "message": f"评论成功：{(message or '')[:30]}"}
        if code == 12051:
            return {"ok": True, "action": "comment", "message": "已有相同评论"}
        return {"ok": False, "action": "comment",
                "message": f"评论失败 code={code} {_api_message(p)}".strip()}
    except Exception as e:
        return {"ok": False, "action": "comment", "message": f"评论异常: {e}"}


def execute_participation(
    client: bili_client.BiliClient,
    *,
    dynamic_id: str,
    sender_uid: str = "",
    comment_text: str = "",
    comment_rid: str = "",
    comment_type: int = 17,
    steps: tuple = ("like", "follow", "repost", "comment"),
    dry_run: bool = False,
    on_step=None,
) -> dict:
    """执行参与互动（点赞/关注/转发/评论），返回 {results: [...], ok, errors}

    steps 可配置要执行的动作顺序（默认 like -> follow -> repost -> comment）。
    dry_run=True 时只探测状态不真正执行（用于检查/预演）。
    on_step: 可选回调 on_step(step_index, total_steps, action_name, detail)，
            每步开始前调用（对齐 bilibinggo execute_full_participation 的 report_step）。
    """
    referer = f"https://www.bilibili.com/opus/{dynamic_id}"
    csrf = get_csrf(client)
    my_uid = get_my_uid(client)
    if not csrf:
        return {"ok": False, "results": [],
                "errors": ["未登录或缺少 csrf（bili_jct），请重新扫码登录账号"]}

    results = []
    errors = []
    total = len([s for s in steps if s in ("like", "follow", "repost", "comment")])
    idx = 0
    for step in steps:
        if step not in ("like", "follow", "repost", "comment"):
            continue
        idx += 1
        if on_step:
            on_step(idx, total, step,
                    f"{ACTION_LABELS.get(step, step)}（{idx}/{total}）")
        if step == "like":
            res = {"ok": True, "action": "like", "message": "将点赞"} if dry_run \
                else like_dynamic(client, dynamic_id=dynamic_id, csrf=csrf, referer=referer)
        elif step == "follow":
            res = {"ok": True, "action": "follow", "message": f"将关注 uid={sender_uid}"} if dry_run \
                else follow_user(client, uid=sender_uid, csrf=csrf, referer=referer)
        elif step == "repost":
            res = {"ok": True, "action": "repost", "message": f"将转发 {comment_text[:30]}"} if dry_run \
                else repost_dynamic(client, dynamic_id=dynamic_id, my_uid=my_uid,
                                    csrf=csrf, referer=referer, content=comment_text)
        elif step == "comment":
            res = {"ok": True, "action": "comment", "message": f"将评论 {comment_text[:30]}"} if dry_run \
                else comment_dynamic(client, rid=comment_rid, comment_type=comment_type,
                                     message=comment_text, csrf=csrf, referer=referer)
        results.append(res)
        if not res.get("ok"):
            errors.append(f"{ACTION_LABELS.get(step, step)}：{res.get('message', '')}")
        if not dry_run:
            time.sleep(_action_gap())

    return {"ok": len(errors) == 0, "results": results, "errors": errors}
