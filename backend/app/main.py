"""B 站自动化抽奖助手 - FastAPI 入口

对齐 bilibinggo 契约的本地控制台 API。
"""
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import Base, engine, SessionLocal, get_db
from . import models
from .routers import accounts, activities, auto, logs, monitor, scan, settings
from .services import bili_client

Base.metadata.create_all(bind=engine)


def migrate():
    """轻量表结构迁移：为旧库补充新增列 / 清理废弃列"""
    import sqlite3
    from .database import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
        if "participated_accounts" not in cols:
            conn.execute(
                "ALTER TABLE activities ADD COLUMN participated_accounts TEXT DEFAULT '[]'")
        if "end_time" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN end_time DATETIME")
        if "comment_text" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN comment_text TEXT DEFAULT ''")
        if "reviewed_at" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN reviewed_at DATETIME")
        # 回填：旧数据无结束时间，默认 = 发布时间 + 7 天
        conn.execute(
            "UPDATE activities SET end_time = datetime(publish_time, '+7 day') "
            "WHERE end_time IS NULL AND publish_time IS NOT NULL")
        # 清理废弃列：cover 已从模型移除（前端/后端均不使用）
        if "cover" in cols:
            try:
                conn.execute("ALTER TABLE activities DROP COLUMN cover")
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception:
        pass


migrate()

app = FastAPI(title="B站抽奖助手 API", version="1.0.0",
              description="哔哩哔哩自动化抽奖助手（对齐 bilibinggo 控制台契约）")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts.router)
app.include_router(monitor.router)
app.include_router(activities.router)
app.include_router(logs.router)
app.include_router(settings.router)
app.include_router(scan.router)
app.include_router(auto.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "name": "bili-lottery-assistant", "version": "1.0.0"}


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)):
    """概览统计（对齐 bilibinggo /api/summary）"""
    accounts_count = db.query(models.Account).count()
    active_accounts = db.query(models.Account).filter_by(status="active").count()
    watch_count = db.query(models.MonitorUser).count()
    act_count = db.query(models.Activity).count()
    pending = db.query(models.Activity).filter_by(status="pending").count()
    participated = db.query(models.Activity).filter_by(status="participated").count()
    log_count = db.query(models.Log).count()
    return {
        "accounts": accounts_count, "active_accounts": active_accounts,
        "watch_users": watch_count, "activities": act_count,
        "pending": pending, "participated": participated,
        "logs": log_count,
    }


# ---------------------------------------------------------------------------
# 默认设置注入（首次启动补齐，不影响真实数据）
# ---------------------------------------------------------------------------


def seed_default_settings():
    """启动时补齐缺失的默认设置项（不覆盖已有值、不注入演示数据）"""
    db = SessionLocal()
    try:
        existing = {r.key for r in db.query(models.Setting).all()}
        added = False
        for k, v in settings.DEFAULT_SETTINGS.items():
            if k not in existing:
                db.add(models.Setting(key=k, value=str(v)))
                added = True
        if added:
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_default_settings()
    db = SessionLocal()
    try:
        # 启动时清理：已过期的 pending/participated 活动标记 ended（防止统计口径不一致）
        from datetime import datetime as _dt
        _now = _dt.now()
        changed = (db.query(models.Activity)
                   .filter(models.Activity.status.in_(["pending", "participated"]),
                           models.Activity.end_time.isnot(None),
                           models.Activity.end_time < _now)
                   .update({"status": "ended"}, synchronize_session=False))
        if changed:
            db.commit()
            logs.add_log(db, "info", "activity", f"启动清理：{changed} 个过期活动标记已结束")
        else:
            db.commit()
        logs.add_log(db, "info", "system", "B站抽奖助手后端启动完成")
    finally:
        db.close()
    # 启动全自动模式的定时调度线程（即使未手动启动，设置了定时也会自动开始）
    try:
        from .services.auto_service import auto_manager
        auto_manager._ensure_scheduler()
    except Exception:
        pass
