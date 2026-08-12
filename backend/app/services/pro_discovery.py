"""职业抽奖账号发现：分析抽奖活动评论区用户，判定是否为职业抽奖账号。

逻辑（用户需求）：
1. 拉取抽奖活动评论区用户列表
2. 查看每个用户的空间动态（转发）
3. 若转发的动态中 >= PRO_RATIO(70%) 是抽奖活动 -> 判定为职业抽奖账号
4. 自动加入监控列表（monitor_type=repost）

执行耗时较长（拉评论 + 逐用户空间分析），采用异步模式：
POST 启动后立即返回，前端轮询 GET /api/discover-pro/progress 取结果。
"""
import threading
import time
from datetime import datetime

from ..database import SessionLocal
from .. import models as _models
from . import bili_client
from .participate_text_service import fetch_reply_users

PRO_RATIO = 0.7          # 转发中抽奖占比阈值（>= 视为职业号）
MIN_SAMPLES = 5          # 转发样本数下限（太少不判定，防误判）
SCAN_SLEEP = 1.0         # 每用户扫描间隔（防风控）
MAX_PAGES = 4            # 每个用户空间翻页数
SINCE_DAYS = 30          # 回溯天数

# 职业号判定的强抽奖关键词：比通用 LOTTERY_KEYWORDS 更严格——
# 去掉"福利""送会员"等泛词（营销/广告转发也常带），避免普通营销号被误判为职业号
PRO_STRONG_KEYWORDS = [
    "抽奖", "转发抽奖", "转发关注", "关注转发", "转发送", "抽送", "抽一个",
    "中奖", "评论抽", "留言抽", "点赞抽", "随机抽", "抽三名", "抽两位", "抽三位",
    "抽5位", "抽3位", "抽2位", "抽10位", "抽一位", "抽一名",
    "关注+转发", "转发+点赞", "转发+关注", "抽5人", "抽3人", "抽2人",
]


def _strong_lottery(it) -> bool:
    """职业号判定用：要求含明确抽奖特征（强关键词），排除纯营销/福利泛词。"""
    t = ((it.get("title") or "") + " " + (it.get("desc") or "")).lower()
    return any(kw in t for kw in PRO_STRONG_KEYWORDS)


def _pick_candidate_activity(db) -> int | None:
    """选一个待参与且未做过职业号发现的活动（自动模式冷却期用）。

    按 id 降序（最新入库优先），跳过已发现过的，避免重复分析同一活动。
    """
    act = (db.query(_models.Activity)
           .filter(_models.Activity.status == "pending",
                   _models.Activity.pro_discovered_at.is_(None))
           .order_by(_models.Activity.id.desc())
           .first())
    return act.id if act else None


def discover_pro_users(db, activity_id: int,
                       min_ratio: float = PRO_RATIO,
                       max_users: int = 15,
                       mark_discovered: bool = True) -> dict:
    """对指定活动执行职业抽奖号发现，自动把判定用户加入监控列表。

    返回: {"found": [...], "added": [...], "skipped": [...]}
    mark_discovered=True（默认）：正常完成后标记活动的 pro_discovered_at，
    避免反复对同一活动发现（被外部 stop 提前结束时不标记）。
    """
    from .bili_client import cookies_from_json

    act = db.get(_models.Activity, activity_id)
    if not act:
        return {"found": [], "added": [], "skipped": [], "message": "活动不存在"}

    # 用第一个 active 账号的登录态拉评论（评论需登录才完整）
    acc = (db.query(_models.Account)
           .filter_by(status="active").order_by(_models.Account.id.asc()).first())
    if not acc or not acc.cookies:
        return {"found": [], "added": [], "skipped": [],
                "message": "请先登录至少一个账号"}
    client = bili_client.BiliClient(cookies_from_json(acc.cookies))

    # 1. 拉评论区用户
    try:
        users = fetch_reply_users(client, act.activity_id)
    except Exception:
        users = []
    if not users:
        return {"found": [], "added": [], "skipped": [],
                "message": "未获取到评论区用户"}

    # 2. 逐个分析空间动态比例
    found = []      # 判定为职业号（含统计）
    added = []      # 成功加入监控
    skipped = []    # 已是监控/分析失败/不达标
    existing_uids = {u.uid for u in db.query(_models.MonitorUser).all()}

    for u in users[:max_users]:
        if _stop_event.is_set():
            # 被外部请求暂停（如自动模式下一轮开始）：提前结束，不标记完成
            break
        if u["uid"] in existing_uids:
            skipped.append({"uid": u["uid"], "uname": u["uname"], "reason": "已在监控列表"})
            continue
        try:
            items = client.get_space_dynamics(
                u["uid"], username=u["uname"], source_type="repost",
                max_pages=MAX_PAGES, since_days=SINCE_DAYS,
                only_lottery=False)
            total = len(items)
            lottery = sum(1 for it in items if _strong_lottery(it))
            if total < MIN_SAMPLES:
                skipped.append({"uid": u["uid"], "uname": u["uname"],
                                "reason": f"转发样本不足({total}条)"})
                continue
            ratio = lottery / total if total else 0
            if ratio >= min_ratio:
                found.append({"uid": u["uid"], "uname": u["uname"],
                              "total": total, "lottery": lottery, "ratio": round(ratio, 2)})
                # 3. 自动加入监控列表（拉取头像，避免列表头像空白）
                if not db.query(_models.MonitorUser).filter_by(uid=u["uid"]).first():
                    avatar = ""
                    uname = u["uname"]
                    try:
                        info = client.get_user_space(u["uid"])
                        avatar = info.get("avatar", "")
                        uname = info.get("username") or uname
                    except Exception:
                        pass   # 头像拉取失败不阻塞加入
                    db.add(_models.MonitorUser(
                        uid=u["uid"], username=uname, avatar=avatar,
                        monitor_type="repost", note="职业抽奖号(自动发现)"))
                    db.commit()
                    existing_uids.add(u["uid"])
                    added.append({"uid": u["uid"], "uname": uname,
                                  "total": total, "lottery": lottery,
                                  "ratio": round(ratio, 2)})
            else:
                skipped.append({"uid": u["uid"], "uname": u["uname"],
                                "reason": f"抽奖占比{ratio:.0%}<{min_ratio:.0%}"})
        except Exception as e:
            skipped.append({"uid": u["uid"], "uname": u["uname"],
                            "reason": f"分析失败: {str(e)[:40]}"})
        time.sleep(SCAN_SLEEP)

    # 正常完成（未被暂停）→ 标记活动已做过职业号发现
    if mark_discovered and not _stop_event.is_set():
        try:
            act.pro_discovered_at = datetime.now()
            db.commit()
        except Exception:
            pass

    return {"found": found, "added": added, "skipped": skipped,
            "message": f"发现 {len(found)} 个职业抽奖号，新增 {len(added)} 个监控"}


# ---------------------------------------------------------------------------
# 异步管理器（发现耗时数分钟，POST 立即返回 + 前端轮询进度）
# ---------------------------------------------------------------------------

_state = {
    "running": False,
    "activity_id": None,
    "message": "未启动",
    "result": None,
    "paused_by_auto": False,   # 自动模式轮次开始时请求暂停（下个用户前生效）
}
_lock = threading.Lock()
# 停止信号：自动模式新轮次开始置位，职业号发现线程在用户间检查并提前结束
_stop_event = threading.Event()


def start_discovery(activity_id: int) -> tuple[bool, str]:
    """启动异步职业号发现（单任务）"""
    _stop_event.clear()
    with _lock:
        if _state["running"]:
            return False, "职业号发现已在进行中"
        _state.update(running=True, activity_id=activity_id,
                      message="准备中...", result=None,
                      paused_by_auto=False)
    threading.Thread(target=_run, args=(activity_id,), daemon=True).start()
    return True, "已启动职业抽奖号发现（后台分析，可稍后查看结果）"


def stop_discovery() -> bool:
    """请求暂停职业号发现（自动模式轮次开始时调用，避免与参与/扫描同时请求 B 站）。

    线程在下一个用户分析前检查信号提前结束；返回当时是否在运行。
    """
    was_running = _state.get("running", False)
    _stop_event.set()
    with _lock:
        if was_running:
            _state["paused_by_auto"] = True
            _state["message"] = "已暂停（自动模式轮次进行中）"
    return was_running


def _run(activity_id: int):
    from ..routers.logs import add_log
    db = SessionLocal()
    try:
        result = discover_pro_users(db, activity_id)
        found = len(result.get("found") or [])
        added = len(result.get("added") or [])
        with _lock:
            _state["result"] = result
            if _stop_event.is_set():
                _state["message"] = f"已暂停（分析到第 {len(result.get('skipped') or [])} 个用户）"
                _state["paused_by_auto"] = True
            else:
                _state["message"] = result.get("message", "完成")
        if _stop_event.is_set():
            add_log(db, "info", "activity",
                    f"职业号发现已暂停（自动模式轮次开始）")
        elif added:
            add_log(db, "success", "activity",
                    f"职业号发现完成：{result.get('message')}（"
                    f"发现 {found} 个，新增监控 {added} 个）")
        else:
            add_log(db, "info", "activity",
                    f"职业号发现完成：未发现新职业号"
                    f"（{result.get('message', '')}）")
    except Exception as e:
        with _lock:
            _state["result"] = {"found": [], "added": [], "skipped": [],
                                "message": f"发现异常: {e}"}
            _state["message"] = f"发现异常: {e}"
        add_log(db, "error", "activity", f"职业号发现异常（活动 {activity_id}）: {e}")
    finally:
        db.close()
        with _lock:
            _state["running"] = False


def get_discovery_progress() -> dict:
    with _lock:
        return {"running": _state["running"],
                "activity_id": _state["activity_id"],
                "message": _state["message"],
                "paused_by_auto": _state.get("paused_by_auto", False),
                "result": _state["result"]}
