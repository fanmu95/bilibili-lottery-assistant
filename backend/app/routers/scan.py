"""扫描接口：启动/停止批量扫描、查询扫描进度（进度条轮询）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .. import schemas
from ..services.scan_service import scan_manager

router = APIRouter(prefix="/api/scan", tags=["scan"])


@router.post("/start")
def start_scan(body: schemas.ScanRequest):
    ok, msg = scan_manager.start(body.user_ids)
    return {"ok": ok, "message": msg}


@router.post("/stop")
def stop_scan():
    scan_manager.stop()
    return {"ok": True, "message": "已请求停止扫描"}


@router.get("/progress")
def scan_progress():
    return scan_manager.progress
