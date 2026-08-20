"""B 站自动化抽奖助手 - FastAPI 入口

对齐 bilibinggo 契约的本地控制台 API。
"""
import os
import sys
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import Base, engine, SessionLocal, get_db
from . import models
from .routers import accounts, activities, auto, cleanup, logs, monitor, scan, settings, update
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
        if "participated_at_map" not in cols:
            conn.execute(
                "ALTER TABLE activities ADD COLUMN participated_at_map TEXT DEFAULT '{}'")
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

# 全局异常日志：任何接口 500 时把完整 traceback 写入 data/app.log（exe 打包
# 后 stdout 已重定向到 app.log；dev 环境直接打控制台），便于用户反馈定位
import logging as _logging
_logger = _logging.getLogger("bili.error")


@app.exception_handler(Exception)
async def _global_500(request, exc):
    from fastapi.responses import JSONResponse
    _logger.exception("接口异常 %s %s: %s",
                      request.method, request.url.path, exc)
    return JSONResponse(status_code=500,
                        content={"detail": f"服务器内部错误：{exc}"})

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
app.include_router(update.router)
app.include_router(auto.router)
app.include_router(cleanup.router)


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
# 生产模式：托管前端构建产物（dist），单端口访问（浏览器打开 http://localhost:8000）
#  - exe 打包：dist 内嵌进可执行文件（sys._MEIPASS/dist）
#  - Docker：dist 复制进镜像（frontend/dist）
#  - 源码运行：定位项目内 frontend/dist（构建过才有）
# ---------------------------------------------------------------------------
def _find_frontend_dist() -> str | None:
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(os.path.join(sys._MEIPASS, "dist"))
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(_here, "..", "frontend", "dist"))      # backend/../frontend/dist
    candidates.append(os.path.join(_here, "..", "..", "frontend", "dist"))
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "index.html")):
            return c
    return None


_DIST = _find_frontend_dist()
if _DIST:
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    _assets_dir = os.path.join(_DIST, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    # SPA 回退：仅当请求未匹配任何 API 路由（404）时返回前端页面。
    # 用 404 处理器而不是 catch-all 通配路由——通配路由会与 /api/xxx/{id}
    # 这类带路径参数的路由产生匹配顺序冲突（部分请求被错误拦截 404）。
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    @app.exception_handler(_StarletteHTTPException)
    async def _spa_404(request, exc):
        if exc.status_code == 404 and not request.url.path.startswith("/api"):
            rel = request.url.path.lstrip("/")
            f = os.path.join(_DIST, rel)
            # index.html 不缓存：dist 重建后 chunk 文件名变化，缓存旧 index.html
            # 会引用已删除的旧 chunk 导致前端资源 404/渲染异常
            _headers = {"Cache-Control": "no-cache"}
            if rel and os.path.isfile(f):
                return FileResponse(f, headers=_headers)
            return FileResponse(os.path.join(_DIST, "index.html"),
                                headers=_headers)
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")},
                            status_code=exc.status_code)


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
        # 全新环境导入打包配置（设置项 + 监控用户；不含 cookie/API key）
        try:
            from .pack_config import import_pack_config
            rst = import_pack_config(db)
            if rst.get("settings") or rst.get("users"):
                logs.add_log(
                    db, "info", "system",
                    f"已导入随包配置：设置 {rst['settings']} 项，"
                    f"监控用户 {rst['users']} 个")
        except Exception:
            pass
        # 启动时补齐新增列（SQLite ALTER TABLE，列不存在才加）
        try:
            from sqlalchemy import text
            _new_cols = {
                "activities": [("pro_discovered_at", "DATETIME"),
                               ("cleanup_deleted_at", "DATETIME")],
                "monitor_users": [("empty_scan_count", "INTEGER"),
                                  ("scanned_count", "INTEGER")],
            }
            for _tbl, _cols in _new_cols.items():
                _cols_exist = {r[1] for r in db.execute(
                    text(f"PRAGMA table_info({_tbl})")).fetchall()}
                for _cname, _ctype in _cols:
                    if _cname not in _cols_exist:
                        db.execute(text(
                            f"ALTER TABLE {_tbl} ADD COLUMN {_cname} {_ctype}"))
                        db.commit()
                        logs.add_log(db, "info", "system",
                                     f"数据库补列：{_tbl}.{_cname}")
        except Exception:
            pass
        # 启动时应用 B 站全局限流速率（bili_rps 设置；设置保存时也会热更新）
        try:
            from .services.rate_limit import configure_rate_limit
            from .services import settings as _s
            _rps = None
            row = db.query(models.Setting).filter_by(key="bili_rps").first()
            if row:
                try:
                    _rps = float(row.value)
                except (TypeError, ValueError):
                    _rps = None
            configure_rate_limit(_rps)
        except Exception:
            pass
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
