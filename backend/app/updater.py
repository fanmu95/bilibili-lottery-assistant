"""版本检查与自动更新。

- get_current_version()：读取 exe 同级 version.json（CI 打包时写入）；开发环境返回 dev
- check_update()：调 GitHub Releases API 对比最新版本（失败静默返回 None）
- 更新流程：下载 zip 到 data/update/ → 生成 update.bat → 退出主程序，
  bat 清除旧程序文件（保留 data/ 用户数据）→ 解压覆盖 → 重启
"""
import json
import logging
import os
import re
import sys

import requests

logger = logging.getLogger("bili.updater")

REPO = "fanmu95/lottery-assistant"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


def get_app_base() -> str:
    """程序根目录：exe 场景 = exe 所在目录；开发 = backend 上一级"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_current_version() -> str:
    """当前版本：exe 同级 version.json（CI 写入）；无则 dev"""
    try:
        p = os.path.join(get_app_base(), "version.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return str(json.load(f).get("version") or "0.0.0")
    except Exception:
        pass
    return "dev"


def is_docker() -> bool:
    """是否为 Docker 容器环境（Dockerfile 设置了 BILI_DATA_DIR=/app/data）。

    Docker 端更新方式 = docker compose pull && up -d（镜像重建），
    容器内 bat 覆盖方案不适用——前端据此隐藏下载/立即更新按钮。
    """
    try:
        if os.environ.get("BILI_DATA_DIR"):
            return True
        return sys.platform != "win32"
    except Exception:
        return False


def parse_version(v: str) -> tuple:
    """'v0.3.1' / '0.3.1' -> (0,3,1)；解析失败返回 None"""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(v) or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def check_update(timeout: int = 10, url: str | None = None) -> dict | None:
    """检查最新版本。返回：
    {current, latest, has_update, download_url, release_url}
    检查源优先级：传入 url（设置项 update_check_url）> GitHub Releases API。
    私有仓库的 GitHub API 匿名访问返回 404 → 静默无提示；
    可配置一个公开 JSON 端点 {version, download_url, release_url}。
    网络失败/无新版本返回 None/无更新；绝不抛异常（静默）。
    """
    try:
        cur = get_current_version()
        latest, dl, rel = "", "", ""
        if url:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                latest = str(d.get("version") or "")
                dl = str(d.get("download_url") or "")
                rel = str(d.get("release_url") or "")
        else:
            r = requests.get(API_LATEST, timeout=timeout,
                             headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                d = r.json()
                latest = str(d.get("tag_name") or "")
                for a in (d.get("assets") or []):
                    if a.get("name", "").endswith("-windows-x64.zip"):
                        dl = a.get("browser_download_url", "")
                        break
                rel = d.get("html_url", "")
        cur_v = parse_version(cur)
        latest_v = parse_version(latest)
        has = bool(latest_v and (cur_v is None or latest_v > cur_v))
        return {
            "current": cur, "latest": latest, "has_update": has,
            "download_url": dl, "release_url": rel,
        }
    except Exception as e:
        logger.warning("版本检查失败: %s", e)
        return None
