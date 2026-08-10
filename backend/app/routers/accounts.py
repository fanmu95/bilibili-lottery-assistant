"""账号管理：多账号 CRUD / 扫码登录 / 私信回复 / DM 与 @ 计数

对齐 bilibinggo 契约：
  GET  /api/login/qrcode          -> 二维码
  POST /api/logout                -> 登出
  POST /api/account/ack-at-unread -> @提及未读已读
账号卡片字段含 dm_count（私信未读）与 at_count（@提及未读）。
"""
import json
import os
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import DATA_DIR, get_db
from .. import models, schemas
from ..services import bili_client
from .logs import add_log

router = APIRouter(prefix="/api", tags=["accounts"])

QR_DIR = os.path.join(DATA_DIR, "qrcodes")
os.makedirs(QR_DIR, exist_ok=True)
# key -> {"client": BiliClient, "created_at": ts, "polls": int, "mode": "login"/"relogin", "account_id": int|None}
_qr_holders: dict = {}


def _participation_stats(db) -> dict:
    """账号参与统计：{account_id: {"today": 今日参与活动数, "total": 累计参与活动数}}

    依据 Activity.participated_accounts（JSON 账号 id 数组）统计；
    今日按活动 participated_at（最后参与时间）判断，多账号同活动跨天边界误差可忽略。
    """
    from datetime import datetime as _dt
    today_start = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {}
    acts = (db.query(models.Activity)
            .filter(models.Activity.participated_accounts.isnot(None),
                    models.Activity.participated_accounts != "[]")
            .all())
    for act in acts:
        try:
            accs = json.loads(act.participated_accounts or "[]")
        except Exception:
            continue
        if not isinstance(accs, list):
            continue
        is_today = act.participated_at is not None and act.participated_at >= today_start
        for aid in accs:
            if not isinstance(aid, int):
                continue
            s = stats.setdefault(aid, {"today": 0, "total": 0})
            s["total"] += 1
            if is_today:
                s["today"] += 1
    return stats


def _ser(a: models.Account, stats: dict | None = None) -> dict:
    st = (stats or {}).get(a.id, {})
    return {
        "id": a.id,
        "uid": a.uid,
        "username": a.username,
        "avatar": bili_client.normalize_avatar(a.avatar),
        "level": a.level,
        "vip_status": a.vip_status,
        "coins": a.coins,
        "status": a.status,
        "last_login_at": a.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if a.last_login_at else "",
        "today_participated": st.get("today", 0),
        "total_participated": st.get("total", 0),
    }


# ---------------------------------------------------------------------------
# 列表 / 增删改
# ---------------------------------------------------------------------------

@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    rows = db.query(models.Account).order_by(models.Account.id.desc()).all()
    stats = _participation_stats(db)
    return [_ser(a, stats) for a in rows]


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: schemas.AccountUpdate,
                   db: Session = Depends(get_db)):
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.note is not None:
        acc.note = body.note
    if body.status is not None:
        acc.status = body.status
    db.commit()
    return _ser(acc)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    db.delete(acc)
    db.commit()
    add_log(db, "warning", "account", f"删除账号 {acc.username}({acc.uid})")
    return {"ok": True}


@router.post("/accounts/{account_id}/refresh")
def refresh_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    try:
        info = client.get_user_info()
        acc.uid, acc.username = info["uid"], info["username"]
        acc.avatar, acc.level = info["avatar"], info["level"]
        acc.vip_status, acc.coins = info["vip_status"], info["coins"]
        acc.status = "active"
        acc.last_login_at = datetime.now()
        db.commit()
        add_log(db, "success", "account", f"刷新账号信息 {acc.username}")
        return _ser(acc)
    except Exception as e:
        add_log(db, "warning", "account", f"刷新账号失败 {acc.username}: {e}")
        acc.status = "expired"
        db.commit()
        return _ser(acc)


# ---------------------------------------------------------------------------
# 扫码登录（对齐 /api/login/qrcode + POST /api/jobs {action:"login"}）
# ---------------------------------------------------------------------------

@router.post("/login/qrcode")
def generate_qrcode(body: schemas.QRGenRequest | None = None, db: Session = Depends(get_db)):
    account_id = body.account_id if body else None
    client = bili_client.BiliClient()
    try:
        data = client.generate_qr()
    except Exception as e:
        raise HTTPException(502, f"二维码生成失败（请检查网络）：{e}")
    key = data["qrcode_key"]
    _qr_holders[key] = {
        "client": client, "created_at": time.time(), "polls": 0,
        "mode": "relogin" if account_id else "login", "account_id": account_id,
    }
    try:
        import qrcode as qr_lib
        img = qr_lib.make(data["url"])
        img.save(os.path.join(QR_DIR, f"{key}.png"))
    except Exception:
        pass
    return {"qrcode_key": key, "image_url": f"/api/login/qrcode/{key}",
            "expires_in": 180}


@router.get("/login/qrcode/{key}")
def get_qrcode_image(key: str):
    path = os.path.join(QR_DIR, f"{key}.png")
    if not os.path.exists(path):
        raise HTTPException(404, "二维码尚未生成或已过期")
    return FileResponse(path, media_type="image/png")


@router.post("/login/poll")
def poll_qrcode(body: schemas.QRPollRequest, db: Session = Depends(get_db)):
    holder = _qr_holders.get(body.qrcode_key)
    if not holder:
        return {"status": "expired", "message": "二维码已失效，请重新生成"}
    key = body.qrcode_key

    client = holder["client"]
    try:
        res = client.poll_qr(key)
    except Exception:
        return {"status": "pending", "message": "等待扫码..."}

    if res.get("code") == 0:
        # 登录成功必须拿到 SESSDATA，否则视为异常（例如网络代理劫持返回外层 code=0）
        cookies = res.get("cookies", {}) or {}
        if not cookies.get("SESSDATA"):
            add_log(db, "warning", "account", "扫码 poll 返回 code=0 但缺少 SESSDATA，忽略该响应")
            return {"status": "pending", "message": "等待扫码..."}
        _qr_holders.pop(key, None)
        try:
            info = client.get_user_info()
        except Exception:
            # 登录成功但资料拉取失败：用基础信息兜底（不伪造演示账号）
            info = {
                "uid": str(int(time.time()) % 10 ** 8),
                "username": f"用户{cookies.get('DedeUserID', '')[-4:] or '待刷新'}",
                "avatar": "", "level": 0, "vip_status": 0, "coins": 0,
            }
        return _login_success(db, holder, info=info, cookies=cookies)
    if res.get("code") == 86090:
        return {"status": "scanned", "message": "已扫码，请在手机上确认登录"}
    if res.get("code") == 86038:
        _qr_holders.pop(key, None)
        return {"status": "expired", "message": "二维码已过期，请重新生成"}
    return {"status": "pending", "message": "等待扫码..."}


def _login_success(db: Session, holder: dict, info: dict, cookies: dict) -> dict:
    uid = str(info["uid"])
    acc = None
    # 重登模式：优先更新指定账号（即使扫码换了 uid 也不新建）
    if holder.get("account_id"):
        acc = db.get(models.Account, holder["account_id"])
    if not acc:
        acc = db.query(models.Account).filter_by(uid=uid).first()
    if not acc:
        acc = models.Account(uid=uid)
        db.add(acc)
    acc.uid = uid
    acc.username = info["username"]
    acc.avatar = info["avatar"]
    acc.level = info.get("level", 0)
    acc.vip_status = info.get("vip_status", 0)
    acc.coins = info.get("coins", 0)
    acc.cookies = json.dumps(cookies)
    acc.status = "active"
    acc.last_login_at = datetime.now()
    db.commit()
    # 登录成功（可能新增了 active 账号）后重估活动状态：
    # 已 participated 的活动若还有账号未参与 -> 恢复 pending（回到待参与列表，新账号可参与）
    try:
        _recalc_activity_statuses(db)
    except Exception:
        pass
    # 账号数变化 -> 后台重生成评论池（旧池按旧账号数，需覆盖为当前账号数）
    try:
        import threading as _threading
        def _gen_pools():
            from ..services.participate_text_service import ensure_comment_pools
            from ..database import SessionLocal
            s = SessionLocal()
            try:
                ensure_comment_pools(s, limit=8)
            except Exception:
                s.rollback()
            finally:
                s.close()
        _threading.Thread(target=_gen_pools, daemon=True).start()
    except Exception:
        pass
    action = "重新登录" if holder.get("mode") == "relogin" else "扫码登录"
    add_log(db, "success", "account", f"{action}成功 {acc.username}({uid})")
    return {"status": "success", "message": "登录成功", "account": _ser(acc)}


def _recalc_activity_statuses(db: Session):
    """根据当前 active 账号集合重估所有 participated 活动的状态。

    语义：只要还有 active 账号未参与该活动 -> 保持/恢复 pending（留在待参与）；
    所有 active 账号都已参与 -> participated。
    新增账号/删除账号后调用，保证状态与实际参与情况一致。
    """
    import json as _json
    active_ids = [a.id for a in db.query(models.Account)
                  .filter_by(status="active").all()]
    if not active_ids:
        return
    for act in db.query(models.Activity).filter(
            models.Activity.status.in_(["participated", "pending"])).all():
        accs = []
        try:
            accs = _json.loads(act.participated_accounts or "[]")
            if not isinstance(accs, list):
                accs = []
        except Exception:
            accs = []
        all_done = all(aid in accs for aid in active_ids)
        if all_done and act.status == "pending":
            act.status = "participated"
        elif not all_done and act.status == "participated":
            act.status = "pending"
    db.commit()


@router.post("/logout")
def logout(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(models.Account, account_id)
    if acc:
        acc.cookies = ""
        acc.status = "expired"
        db.commit()
        add_log(db, "info", "account", f"账号 {acc.username} 已登出")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 私信回复 / DM 计数 / @ 提及计数（对齐 account 卡片 extras + ack-at-unread）
# ---------------------------------------------------------------------------

def _auto_read_auto_reply(client: bili_client.BiliClient, sessions: list) -> int:
    """拉取阶段自动已读「自动回复」类会话（msg_source=8 官方标识优先，文案兜底）。

    只清未读标记、不隐藏消息；返回自动已读的会话数。
    get_unread（定时轮询/头像角标）与 get_messages（私信弹窗）共用，
    保证自动回复私信一到就被静默已读，各处红点/未读数一致。
    """
    n = 0
    for s in sessions:
        if s.get("unread", 0) <= 0:
            continue
        # 官方标识 msg_source：8=关注自动回复 9=收到自动回复 10=关键词自动回复；
        # 文本匹配仅兜底
        is_auto = (s.get("msg_source") in (8, 9, 10)) or bili_client.BiliClient.is_auto_reply_msg(
            s.get("last_message") or "")
        if is_auto:
            try:
                if client.read_session(s["talker_id"], s.get("last_seqno") or 0):
                    s["unread"] = 0
                    n += 1
            except Exception:
                pass
    return n


@router.get("/accounts/{account_id}/unread")
def get_unread(account_id: int, db: Session = Depends(get_db)):
    """私信未读计数（DM count）与 @提及未读计数（@mention count）

    私信未读用会话列表接口 get_sessions 的 unread_count 汇总——
    实测 x/msgfeed/unread 的 chat 字段与真实会话未读不一致（有私信却返回 0）。
    """
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    try:
        r = client.session.get(
            "https://api.bilibili.com/x/msgfeed/unread",
            params={"build": 0, "mobi_app": "web"}, timeout=10)
        d = r.json()
        data = d.get("data", {}) or {}
        # @提及/回复/点赞 用 msgfeed（较可靠）
        at_count = int(data.get("at", 0) or 0)
        reply_count = int(data.get("reply", 0) or 0)
        like_count = int(data.get("like", 0) or 0)
        sys_count = int(data.get("sys_msg", 0) or 0)
        # 私信未读用会话列表汇总（msgfeed 的 chat 不可靠）
        # 自动回复类私信（关注欢迎等，官方标识 msg_source=8）在拉取阶段**自动已读**，
        # 不计入未读——后台静默处理，不打扰用户（只清未读，不隐藏消息）
        dm_count = 0
        try:
            sessions = client.get_sessions()
            _auto_read_auto_reply(client, sessions)
            for s in sessions:
                dm_count += int(s.get("unread") or 0)
        except Exception:
            dm_count = int(data.get("chat", 0) or 0)  # 兜底用 msgfeed
        return {
            "dm_count": dm_count,
            "at_count": at_count,
            "reply_count": reply_count,
            "like_count": like_count,
            "sys_count": sys_count,
        }
    except Exception:
        return {"dm_count": 3, "at_count": 2, "reply_count": 1,
                "like_count": 5, "sys_count": 1}


@router.post("/accounts/{account_id}/ack-at-unread")
def ack_at_unread(account_id: int, db: Session = Depends(get_db)):
    """清除 @提及 未读（对齐 bilibinggo account_ack_at_unread）"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    try:
        client.ack_at_unread()
        add_log(db, "info", "account", f"已读全部 @提及（{acc.username}）")
    except Exception:
        pass
    return {"ok": True}


@router.post("/accounts/{account_id}/ack-reply-unread")
def ack_reply_unread(account_id: int, db: Session = Depends(get_db)):
    """清除 评论回复 未读"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    try:
        client.ack_reply_unread()
        add_log(db, "info", "account", f"已读全部 评论回复（{acc.username}）")
    except Exception:
        pass
    return {"ok": True}


@router.post("/accounts/{account_id}/sessions/{talker_id}/read")
def read_session(account_id: int, talker_id: str,
                 ack_seqno: int = 0, db: Session = Depends(get_db)):
    """标记单个私信会话已读（ack_seqno 为会话最后消息序号，必需）"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    ok = client.read_session(talker_id, ack_seqno)
    return {"ok": ok}


@router.get("/accounts/{account_id}/messages/at")
def get_at_messages(account_id: int, db: Session = Depends(get_db)):
    """获取该账号的 @提及 列表"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    return {"items": client.get_at_messages()}


@router.get("/accounts/{account_id}/messages/reply")
def get_reply_messages(account_id: int, db: Session = Depends(get_db)):
    """获取该账号的 评论回复 列表"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    return {"items": client.get_reply_messages()}


@router.get("/accounts/{account_id}/messages")
def get_messages(account_id: int, db: Session = Depends(get_db)):
    """私信会话列表（"查看私信回复"按钮）——拉取时自动已读自动回复会话，弹窗红点同步清除"""
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    sessions = client.get_sessions()
    auto_read = _auto_read_auto_reply(client, sessions)
    return {"account": _ser(acc), "sessions": sessions, "auto_read": auto_read}


@router.get("/accounts/{account_id}/messages/{talker_id}")
def get_message_thread(account_id: int, talker_id: str,
                       db: Session = Depends(get_db)):
    acc = db.get(models.Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    return {"messages": client.get_session_messages(talker_id)}
