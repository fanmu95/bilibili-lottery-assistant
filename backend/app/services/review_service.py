# -*- coding: utf-8 -*-
"""两阶段解析·第二阶段：复核/纠错初判结果（异步、空闲时间执行）。

初次扫描入库后，Activity 只被初判一次（prize/end_time 可能有误或缺失）。
本服务在后台对「未复核」的待参与活动再调一次 LLM：
  原文 + 初判结果 -> 复核 verdict -> 有依据的字段覆盖初判（纠错/补全）
写入规则：
- 复核字段非空 -> 覆盖初判（复核 prompt 强制「正文无依据则输出空」，非空即有依据）
- 复核字段为空 -> 保留初判
- 复核判 is_lottery=false -> 标记 skipped（避免参与非抽奖动态）
"""
import threading

from ..database import SessionLocal
from .. import models

# 单例锁：同一时刻只跑一个复核线程（防止多轮触发并发）
_review_lock = threading.Lock()
_review_running = False

REVIEW_BATCH = 5          # 每批复核条数（思考模式，控制输出量）
REVIEW_LIMIT = 30         # 每轮最多复核条数（批间 3 线程并行，30 条约 40 秒）


def _apply_review(act, verdict: dict) -> bool:
    """把复核 verdict 写入活动（非空字段覆盖初判；空字段保留）。返回是否变更。"""
    from .scan_service import _parse_end_time as _scan_parse
    changed = False
    # prize：复核非空覆盖
    prize = (verdict.get("prize") or "").strip()
    if prize and prize != (act.prize_info or "").strip():
        act.prize_info = prize[:200]
        changed = True
    # end_time：复核非空覆盖（含具体时刻）
    et = _scan_parse((verdict.get("end_time") or "").strip())
    if et and et != act.end_time:
        act.end_time = et
        changed = True
    # winner_count：复核 >0 覆盖
    w = verdict.get("winner_count")
    if isinstance(w, int) and w > 0 and w != act.winner_count:
        act.winner_count = w
        changed = True
    # title：复核非空且更长/更精炼时覆盖
    title = (verdict.get("title") or "").strip()
    if title and len(title) > len(act.title or ""):
        act.title = title[:200]
        changed = True
    return changed


def ensure_reviews(db, limit: int = REVIEW_LIMIT) -> int:
    """找出未复核的待参与活动，批量复核并修正字段。返回修正条数。

    优先级：即将开奖的（end_time 升序）在前；无 end_time 的排在有时间的后面。
    仅 llm_generate / random 模式（已配 LLM）时生效；复核失败不影响主流程。
    """
    from datetime import datetime
    from . import llm_client
    try:
        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
        mode = settings_map.get("participate_text_mode", "custom")
        if mode not in ("llm_generate", "random"):
            return 0
        llm_cfg = {
            "base_url": settings_map.get("llm_base_url", ""),
            "api_key": settings_map.get("llm_api_key", ""),
            "model": settings_map.get("llm_model", ""),
        }
        # 合并当前模型的参数覆盖（llm_model_overrides：temperature/top_p/max_tokens）
        try:
            from . import llm_client as _lc
            llm_cfg.update(_lc.resolve_model_overrides(
                settings_map, llm_cfg.get("model", "")))
        except Exception:
            pass
        if not llm_cfg.get("base_url") or not llm_cfg.get("model"):
            return 0
        now = datetime.now()
        # 未复核 且 待参与（pending/participated 未过期）的活动
        cands = (db.query(models.Activity)
                 .filter(models.Activity.status.in_(["pending", "participated"]),
                         models.Activity.reviewed_at.is_(None),
                         (models.Activity.end_time.is_(None))
                         | (models.Activity.end_time > now))
                 .order_by(models.Activity.end_time.is_(None),
                           models.Activity.end_time.asc())
                 .limit(limit).all())
        if not cands:
            return 0
        items = [{"id": str(a.id),
                  "text": ((a.desc or "") or (a.title or "")),
                  "verdict": {
                      "is_lottery": a.status == "pending",
                      "title": a.title,
                      "prize": a.prize_info,
                      "winner_count": a.winner_count,
                      "end_time": a.end_time.strftime("%Y-%m-%d %H:%M")
                          if a.end_time else "",
                  }}
                 for a in cands]
        results = llm_client.review_parse_verdicts_batch(
            llm_cfg["base_url"], llm_cfg["api_key"], llm_cfg["model"],
            items, batch_size=REVIEW_BATCH,
            temperature=llm_cfg.get("temperature"),
            top_p=llm_cfg.get("top_p"),
            max_tokens=llm_cfg.get("max_tokens"))
        fixed = 0
        skipped = 0
        for a in cands:
            v = results.get(str(a.id))
            if not v:
                continue
            # 复核判非抽奖 -> 标记 skipped（不再参与）
            if v.get("is_lottery") is False and a.status in ("pending", "participated"):
                a.status = "skipped"
                skipped += 1
            elif _apply_review(a, v):
                fixed += 1
            a.reviewed_at = now
        if fixed or skipped:
            db.commit()
        return fixed + skipped
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def ensure_reviews_bg():
    """后台线程入口：独立 session 跑一轮复核（不阻塞主流程）"""
    global _review_running
    if _review_running:
        return
    with _review_lock:
        if _review_running:
            return
        _review_running = True
    try:
        db = SessionLocal()
        try:
            n = ensure_reviews(db)
            if n:
                from ..routers.logs import add_log
                add_log(db, "info", "auto",
                        f"复核修正 {n} 个活动的奖品/时间信息")
        finally:
            db.close()
    finally:
        _review_running = False


def trigger_review_thread() -> bool:
    """触发一轮后台复核（异步，无复核任务/已在跑则静默返回 False）"""
    if _review_running:
        return False
    try:
        t = threading.Thread(target=ensure_reviews_bg, daemon=True)
        t.start()
        return True
    except Exception:
        return False
