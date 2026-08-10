"""日志功能：查询 / 筛选 / 清空（对齐 bilibinggo /api/diagnostics/logs）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models

router = APIRouter(prefix="/api/logs", tags=["logs"])

LOG_MAX_ROWS = 5000    # 日志表上限，超出自动裁剪最旧记录（防无限增长拖慢查询）
_LOG_CLEANUP_TICK = 0


def add_log(db: Session, level: str, module: str, message: str):
    global _LOG_CLEANUP_TICK
    db.add(models.Log(level=level, module=module, message=message))
    db.commit()
    # 每 50 条写入触发一次裁剪检查（避免每次 commit 都 count 拖慢）
    _LOG_CLEANUP_TICK += 1
    if _LOG_CLEANUP_TICK % 50 == 0:
        try:
            total = db.query(models.Log).count()
            if total > LOG_MAX_ROWS:
                oldest_id = (db.query(models.Log.id)
                             .order_by(models.Log.id.desc())
                             .offset(LOG_MAX_ROWS).limit(1).scalar())
                if oldest_id:
                    db.query(models.Log).filter(models.Log.id < oldest_id).delete()
                    db.commit()
        except Exception:
            pass


def _ser(log: models.Log) -> dict:
    return {
        "id": log.id,
        "level": log.level,
        "module": log.module,
        "message": log.message,
        "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("")
def list_logs(page: int = 1, page_size: int = 100,
              level: str = "", module: str = "", keyword: str = "",
              db: Session = Depends(get_db)):
    q = db.query(models.Log)
    if level:
        q = q.filter(models.Log.level == level)
    if module:
        q = q.filter(models.Log.module == module)
    if keyword:
        q = q.filter(models.Log.message.contains(keyword))
    total = q.count()
    items = (q.order_by(models.Log.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "items": [_ser(i) for i in items]}


@router.get("/modules")
def list_modules(db: Session = Depends(get_db)):
    rows = db.query(models.Log.module).distinct().all()
    return [r[0] for r in rows if r[0]]


@router.delete("")
def clear_logs(db: Session = Depends(get_db)):
    db.query(models.Log).delete()
    db.commit()
    return {"ok": True}
