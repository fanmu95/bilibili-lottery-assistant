"""活动发现：监控用户（活动来源）管理

对齐 bilibinggo 契约：
  GET    /api/watch-users           监控名单 + 同步状态
  POST   /api/watch-users           按 MID 添加用户
  DELETE /api/watch-users/{mid}     删除用户
增强：批量导入、扫描单用户、查看空间、两种监控类型（转发/发布）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas
from ..services import bili_client, scan_service
from .logs import add_log

router = APIRouter(prefix="/api/watch-users", tags=["monitor"])


def _ser(u: models.MonitorUser) -> dict:
    return {
        "id": u.id,
        "uid": u.uid,
        "username": u.username,
        "avatar": bili_client.normalize_avatar(u.avatar),
        "monitor_type": u.monitor_type,
        "status": u.status,
        "note": u.note or "",
        "empty_scan_count": u.empty_scan_count or 0,
        "scanned_count": u.scanned_count or 0,
        "last_scanned_at": u.last_scanned_at.strftime("%Y-%m-%d %H:%M:%S") if u.last_scanned_at else "",
    }


def _resolve_space(client: bili_client.BiliClient, uid: str) -> dict:
    """获取用户空间信息（真实接口，失败时抛出以便调用方明确处理，不回退演示数据）"""
    return client.get_user_space(uid)


@router.get("")
def list_watch_users(monitor_type: str = "", keyword: str = "",
                     db: Session = Depends(get_db)):
    q = db.query(models.MonitorUser)
    if monitor_type:
        q = q.filter(models.MonitorUser.monitor_type == monitor_type)
    if keyword:
        q = q.filter(or_(
            models.MonitorUser.username.contains(keyword),
            models.MonitorUser.uid.contains(keyword)))
    rows = q.order_by(models.MonitorUser.id.desc()).all()
    return {"total": len(rows), "items": [_ser(u) for u in rows]}


@router.get("/export")
def export_watch_users(db: Session = Depends(get_db)):
    """导出监控用户：一行一个 UID（与批量导入格式兼容，可直接复制回导入框）"""
    rows = db.query(models.MonitorUser).order_by(models.MonitorUser.id.asc()).all()
    content = "\n".join(u.uid for u in rows if u.uid)
    return PlainTextResponse(
        content,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="watch_users.txt"'})


@router.post("")
def add_watch_user(body: schemas.MonitorUserCreate,
                   db: Session = Depends(get_db)):
    uid = body.uid.strip()
    if not uid.isdigit():
        raise HTTPException(400, "MID 必须为纯数字（B 站空间页数字 ID）")
    exists = db.query(models.MonitorUser).filter_by(uid=uid).first()
    if exists:
        # 存在则更新类型/备注，幂等
        exists.monitor_type = body.monitor_type
        if body.note:
            exists.note = body.note
        db.commit()
        add_log(db, "info", "monitor", f"更新监控用户 {exists.username} 的监控类型")
        return _ser(exists)

    client = bili_client.BiliClient()
    try:
        info = _resolve_space(client, uid)
    except Exception as e:
        raise HTTPException(502, f"获取用户信息失败（MID {uid}）：{e}")
    user = models.MonitorUser(
        uid=uid, username=info.get("username") or uid, avatar=info.get("avatar", ""),
        monitor_type=body.monitor_type, note=body.note)
    db.add(user)
    db.commit()
    add_log(db, "success", "monitor",
            f"添加监控用户 {user.username}({uid}) 类型={body.monitor_type}")
    return _ser(user)


@router.post("/batch")
def batch_add_watch_users(body: schemas.MonitorUserBatchCreate,
                          db: Session = Depends(get_db)):
    client = bili_client.BiliClient()
    added, skipped, failed = [], [], []
    for raw in body.uids:
        uid = raw.strip()
        if not uid.isdigit():
            failed.append({"uid": uid, "reason": "MID 格式非法"})
            continue
        if db.query(models.MonitorUser).filter_by(uid=uid).first():
            skipped.append(uid)
            continue
        try:
            info = _resolve_space(client, uid)
        except Exception as e:
            # 拉取头像/昵称失败：不塞演示数据，明确记录失败原因
            failed.append({"uid": uid, "reason": f"获取用户信息失败: {e}"})
            continue
        user = models.MonitorUser(
            uid=uid, username=info.get("username") or uid,
            avatar=info.get("avatar", ""),
            monitor_type=body.monitor_type, note=body.note)
        db.add(user)
        added.append({"uid": uid, "username": user.username})
    db.commit()
    add_log(db, "success", "monitor",
            f"批量导入完成：新增 {len(added)}，跳过 {len(skipped)}，失败 {len(failed)}")
    return {"added": added, "skipped": skipped, "failed": failed}


@router.post("/batch-delete")
def batch_delete_watch_users(body: schemas.BatchIdsRequest,
                             db: Session = Depends(get_db)):
    users = db.query(models.MonitorUser).filter(
        models.MonitorUser.id.in_(body.ids)).all()
    for u in users:
        db.delete(u)
    db.commit()
    add_log(db, "warning", "monitor", f"批量移除监控用户 {len(users)} 个")
    return {"ok": True, "count": len(users)}


@router.put("/{user_id}")
def update_watch_user(user_id: int, body: schemas.MonitorUserUpdate,
                      db: Session = Depends(get_db)):
    user = db.get(models.MonitorUser, user_id)
    if not user:
        raise HTTPException(404, "监控用户不存在")
    if body.monitor_type is not None:
        user.monitor_type = body.monitor_type
    if body.note is not None:
        user.note = body.note
    if body.status is not None:
        user.status = body.status
    db.commit()
    return _ser(user)


@router.delete("/{user_id}")
def delete_watch_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(models.MonitorUser, user_id)
    if not user:
        raise HTTPException(404, "监控用户不存在")
    db.delete(user)
    db.commit()
    add_log(db, "warning", "monitor", f"移除监控用户 {user.username}({user.uid})")
    return {"ok": True}


@router.post("/{user_id}/scan")
def scan_watch_user(user_id: int, db: Session = Depends(get_db)):
    """异步扫描单个监控用户（用扫描管理器，前端轮询进度展示）

    原同步实现会因抓取动态耗时超过请求超时（如 60s/600s）报错，
    改为异步：立即返回，进度经 /api/scan/progress 轮询。
    """
    user = db.get(models.MonitorUser, user_id)
    if not user:
        raise HTTPException(404, "监控用户不存在")
    ok, msg = scan_service.scan_manager.start(user_ids=[user.id])
    if not ok:
        # 已在扫描中：返回当前状态，不报错
        return {"ok": False, "message": msg, "started": False}
    return {"ok": True, "message": f"开始扫描 {user.username}，可查看扫描进度",
            "started": True, "user_id": user_id}


@router.get("/{user_id}/refresh")
def refresh_watch_user(user_id: int, db: Session = Depends(get_db)):
    """重新拉取该用户的空间信息"""
    user = db.get(models.MonitorUser, user_id)
    if not user:
        raise HTTPException(404, "监控用户不存在")
    client = bili_client.BiliClient()
    info = _resolve_space(client, user.uid)
    user.username = info.get("username") or user.username
    user.avatar = info.get("avatar", user.avatar)
    db.commit()
    return _ser(user)
