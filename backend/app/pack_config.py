"""打包配置导出/导入：设置项 + active 监控用户随包带走。

安全边界：
- **绝不导出**：账号 cookie（Account 表整体排除）、LLM API key
  （llm_api_key 及键名含 cookie/api_key/secret/token/password 的项）、
  扫描断点（scan_resume_done，无跨机意义）
- 导出内容：其余全部设置项 + active 监控用户（uid/用户名/类型/备注，无敏感）
- 导入时机：exe 首次启动（监控用户表为空视为全新环境），
  设置只补缺失键（不覆盖已有），监控用户按 uid 去重追加
"""
import json
import os
import re

SENSITIVE_KEY_RE = re.compile(r"(cookie|api_key|secret|token|password)", re.I)
EXCLUDE_KEYS = {"scan_resume_done", "llm_api_key"}


def _pack_path() -> str:
    """pack_data/config.json 位置：PyInstaller 打包资源（_MEIPASS）
    或开发环境 backend/pack_data/"""
    try:
        import sys
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = os.path.join(base, "pack_data", "config.json")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "pack_data", "config.json")


def export_pack_config(db, path: str | None = None) -> dict:
    """导出非敏感设置 + active 监控用户到 pack_data/config.json，返回数据"""
    from . import models
    settings = {}
    for r in db.query(models.Setting).all():
        if r.key in EXCLUDE_KEYS:
            continue
        if SENSITIVE_KEY_RE.search(r.key):
            continue
        settings[r.key] = r.value
    users = [{
        "uid": u.uid, "username": u.username,
        "monitor_type": u.monitor_type, "note": u.note or "",
    } for u in db.query(models.MonitorUser)
        .filter_by(status="active").all()]
    data = {"settings": settings, "monitor_users": users}
    out = path or _pack_path()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


def import_pack_config(db, path: str | None = None) -> dict:
    """全新环境（监控用户为空）时导入包内配置。返回导入统计。

    - 设置：只补缺失键（已有值不覆盖，尊重用户已配置）
    - 监控用户：按 uid 去重追加
    """
    from . import models
    src = path or _pack_path()
    if not os.path.exists(src):
        return {"settings": 0, "users": 0}
    try:
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"settings": 0, "users": 0}
    # 仅全新环境导入（无监控用户 = 新部署）
    existing_users = db.query(models.MonitorUser).count()
    if existing_users > 0:
        return {"settings": 0, "users": 0, "skipped": "已有监控用户"}

    imported_settings = 0
    existing_keys = {r.key for r in db.query(models.Setting).all()}
    for k, v in (data.get("settings") or {}).items():
        if k in existing_keys:
            continue
        db.add(models.Setting(key=k, value=str(v)))
        imported_settings += 1
    imported_users = 0
    existing_uids = {str(u.uid) for u in db.query(models.MonitorUser).all()}
    for u in (data.get("monitor_users") or []):
        uid = str(u.get("uid") or "").strip()
        if not uid or uid in existing_uids:
            continue
        db.add(models.MonitorUser(
            uid=uid,
            username=str(u.get("username") or "")[:128],
            monitor_type=str(u.get("monitor_type") or "repost"),
            note=str(u.get("note") or "")[:256],
            status="active"))
        imported_users += 1
        existing_uids.add(uid)
    if imported_settings or imported_users:
        db.commit()
    return {"settings": imported_settings, "users": imported_users}
