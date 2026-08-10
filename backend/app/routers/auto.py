"""全自动模式：扫描 + 参与 全自动化（用户需求）

  POST /api/auto/start   启动全自动模式
  POST /api/auto/stop    停止
  GET  /api/auto/progress 进度轮询
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .logs import add_log
from ..services.auto_service import auto_manager

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
def auto_progress():
    return auto_manager.progress
