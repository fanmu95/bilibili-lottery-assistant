"""版本检查 / 自动更新接口"""
import logging
import os
import subprocess
import threading

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, updater
from ..database import get_db

router = APIRouter(prefix="/api/update", tags=["update"])

logger = logging.getLogger("bili.update")

# 下载进度（内存态）：{running, total, downloaded, done, error, path}
_download_state = {"running": False, "total": 0, "downloaded": 0,
                   "done": False, "error": "", "path": ""}
_lock = threading.Lock()


@router.get("/check")
def update_check(db=Depends(get_db)):
    """检查新版本（每次打开页面调用；失败静默，不影响使用）"""
    url = None
    try:
        r = db.query(models.Setting).filter_by(key="update_check_url").first()
        if r and r.value:
            url = str(r.value).strip()
    except Exception:
        pass
    # 「是否检测更新」开关：关闭后不检查、不提醒
    try:
        _enabled = db.query(models.Setting).filter_by(
            key="update_check_enabled").first()
        if _enabled and str(_enabled.value).lower() not in ("true", "1", "yes"):
            return {"current": updater.get_current_version(),
                    "latest": "", "has_update": False,
                    "download_url": "", "release_url": "",
                    "is_docker": updater.is_docker(),
                    "check_enabled": False}
    except Exception:
        pass
    info = updater.check_update(url=url)
    if not info:
        return {"current": updater.get_current_version(),
                "latest": "", "has_update": False,
                "download_url": "", "release_url": "",
                "is_docker": updater.is_docker(),
                "check_enabled": True}
    info["is_docker"] = updater.is_docker()
    info["check_enabled"] = True
    return info


@router.get("/download")
def update_download():
    """后台下载最新版本 zip 到 data/update/（进度轮询 /progress）"""
    info = updater.check_update()
    if not info or not info.get("download_url") or not info.get("has_update"):
        return {"ok": False, "message": "没有可下载的新版本"}
    with _lock:
        if _download_state.get("running"):
            return {"ok": False, "message": "下载已在进行中"}
        _download_state.update(running=True, total=0, downloaded=0,
                               done=False, error="", path="")
    threading.Thread(target=_download_bg, args=(info,), daemon=True).start()
    return {"ok": True, "message": "开始后台下载"}


def _download_bg(info: dict):
    import requests
    url = info["download_url"]
    upd_dir = os.path.join(updater.get_app_base(), "data", "update")
    os.makedirs(upd_dir, exist_ok=True)
    target = os.path.join(upd_dir, f"update-{info['latest']}.zip")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            with _lock:
                _download_state["total"] = total
            dl = 0
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    dl += len(chunk)
                    with _lock:
                        _download_state["downloaded"] = dl
        with _lock:
            _download_state.update(done=True, path=target)
    except Exception as e:
        logger.error("下载更新失败: %s", e)
        with _lock:
            _download_state.update(error=str(e)[:200], running=False)
    finally:
        with _lock:
            _download_state["running"] = False


@router.get("/progress")
def update_progress():
    with _lock:
        return dict(_download_state)


@router.post("/apply")
def update_apply():
    """生成 update.bat 并退出主程序（bat 清除旧程序、解压覆盖、重启）。

    保留 data/ 用户数据（不删除）。
    """
    with _lock:
        if _download_state.get("running"):
            return {"ok": False, "message": "下载尚未完成"}
        zip_path = _download_state.get("path", "")
    if not zip_path or not os.path.exists(zip_path):
        return {"ok": False, "message": "更新包不存在，请先下载"}
    base = updater.get_app_base()
    bat = os.path.join(base, "update.bat")
    try:
        with open(bat, "w", encoding="utf-8") as f:
            f.write(
                "@echo off\r\n"
                "cd /d \"%~dp0\"\r\n"
                "taskkill /IM LotteryAssistant.exe /F >nul 2>&1\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                "del /Q LotteryAssistant.exe >nul 2>&1\r\n"
                "rd /s /q _internal >nul 2>&1\r\n"
                "rd /s /q pack_data >nul 2>&1\r\n"
                "del /Q version.json >nul 2>&1\r\n"
                f"powershell -NoProfile -Command "
                f"\"Expand-Archive -Path '{zip_path}' -DestinationPath '%~dp0' -Force\"\r\n"
                "start \"\" \"%~dp0LotteryAssistant.exe\"\r\n"
                "del /Q \"%~f0\" >nul 2>&1\r\n")
        subprocess.Popen(["cmd", "/c", "start", "", bat],
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    except Exception as e:
        return {"ok": False, "message": f"启动更新失败: {e}"}
    # 主程序退出（bat 接管替换 + 重启）
    import sys as _s
    _s.exit(0)
    return {"ok": True, "message": "正在更新并重启..."}
