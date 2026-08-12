"""全自动模式：扫描 + 参与 全自动化（用户需求）

  POST /api/auto/start   启动全自动模式
  POST /api/auto/stop    停止
  GET  /api/auto/progress 进度轮询
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime

from ..database import get_db
from .logs import add_log
from ..services.auto_service import auto_manager
from .. import models

router = APIRouter(prefix="/api/auto", tags=["auto"])


@router.post("/start")
def auto_start(db: Session = Depends(get_db)):
    ok, msg = auto_manager.start()
    if ok:
        add_log(db, "info", "auto", "全自动模式已启动")
    return {"ok": ok, "message": msg}


@router.post("/stop")
def auto_stop(db: Session = Depends(get_db)):
    ok, msg = auto_manager.stop()
    if ok:
        add_log(db, "info", "auto", "全自动模式停止请求")
    return {"ok": ok, "message": msg}


@router.get("/progress")
def auto_progress(db: Session = Depends(get_db)):
    d = dict(auto_manager.progress)
    # 实时计算待参与数：自动扫描（定时/手动/单用户）入库后立即反映，
    # 不依赖主循环每轮开头的快照（轮次间隔可能很长）
    try:
        now = datetime.now()
        d["pending_count"] = db.query(models.Activity).filter(
            models.Activity.status == "pending",
            (models.Activity.end_time.is_(None))
            | (models.Activity.end_time > now),
        ).count()
    except Exception:
        pass
    # 附加职业号发现状态（自动模式冷却期运行、轮次开始暂停）
    try:
        from ..services.pro_discovery import get_discovery_progress
        d["pro_discovery"] = get_discovery_progress()
    except Exception:
        d["pro_discovery"] = {"running": False, "message": "", "activity_id": None,
                              "paused_by_auto": False, "result": None}
    return d
