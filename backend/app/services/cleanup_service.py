"""转发动态清理：按规则删除账号转发过的抽奖动态（账号维度）。

规则（OR，满足任一即删）：
  规则1：在活动列表且已解析结束日期，结束距今超过 end_days 天 → 删（期间自行检查中奖）
  规则2：转发时间距今超过 forward_days 天 → 直接删（不检查列表/结束日期）
  规则3（可选）：列表外/无结束日期的动态 → LLM 解析结束时间再判定
  白名单（中奖动态 id）优先跳过；删除用扫描到的 repost_id 直删（rm_dynamic）
  支持进度上报（progress dict），供前端轮询展示统计过程与结果。
"""
import threading
from datetime import datetime, timedelta

from ..database import SessionLocal
from .. import models
from . import bili_client

# ---- 进度管理（账号维度，内存态，供前端轮询） ----
_progress_lock = threading.Lock()
_progress = {}          # account_id -> dict


def _init_progress(account_id: int, stage: str = "准备中"):
    with _progress_lock:
        _progress[account_id] = {
            "running": True, "stage": stage,
            "pages": 0, "forwards": 0, "candidates": 0,
            "deleted": 0, "failed": 0, "result": None,
        }


def _set_progress(account_id: int, **kw):
    with _progress_lock:
        if account_id in _progress:
            _progress[account_id].update(kw)


def get_progress(account_id: int) -> dict:
    with _progress_lock:
        p = _progress.get(account_id)
    if not p:
        return {"running": False, "stage": "未开始", "result": None}
    return dict(p)


def _finish_progress(account_id: int, result: dict):
    with _progress_lock:
        if account_id in _progress:
            _progress[account_id].update(
                {"running": False, "stage": "完成", "result": result})


def _parse_winners(notice: dict) -> set:
    """从 lottery_result 提取中奖 uid 集合。

    lottery_result 结构（对齐 bilibinggo）：dict {档位_key: [{uid, name}, ...]}，
    如 {"first_prize_result": [{"uid": 123, "name": "xxx"}, ...]}。
    """
    winners = set()
    try:
        result = notice.get("lottery_result") or {}
        if isinstance(result, dict):
            for entries in result.values():
                if not isinstance(entries, list):
                    continue
                for it in entries:
                    if isinstance(it, dict):
                        try:
                            winners.add(str(int(it.get("uid") or 0)))
                        except (TypeError, ValueError):
                            continue
    except Exception:
        pass
    return winners


def _notice_ended(notice: dict) -> bool:
    """notice 是否已开奖：有中奖名单 或 status!=0"""
    if not notice:
        return False
    try:
        if notice.get("lottery_result"):
            return True
        return int(notice.get("status") or 0) != 0
    except (TypeError, ValueError):
        return False


def _settings_map(db) -> dict:
    return {r.key: r.value for r in db.query(models.Setting).all()}


def _open_days_ago(db, act, notice, sm: dict):
    """开奖距今是否超过 N 天（cleanup_end_days，默认 15）。

    开奖日期来源：act.end_time 优先，其次 notice 的 lottery_time（官方开奖时间）。
    返回 True=超过 N 天可清理；False=不够久；None=无法判定（无开奖日期）。
    """
    end = act.end_time
    if end is None and notice:
        end = bili_client.BiliClient.notice_end_time(notice)
    if end is None:
        return None
    try:
        days = int(float(sm.get("cleanup_end_days", 15) or 0))
    except (TypeError, ValueError):
        days = 15
    if days <= 0:
        return True                # 0 = 不限，已开奖即可清
    return (datetime.now() - end).total_seconds() >= days * 86400


def cleanup_transfers(db, dry_run: bool = True) -> dict:
    """主流程：扫描已开奖未中奖的转发动态，预览（dry_run）或删除。

    返回统计：{"checked", "won_keep", "no_notice", "not_ended",
               "to_delete"/"deleted", "failed", "items": [...]}
    """
    sm = _settings_map(db)
    now = datetime.now()
    stat = {"checked": 0, "won_keep": 0, "no_notice": 0, "not_ended": 0,
            "to_delete": 0, "deleted": 0, "failed": 0, "skipped": 0, "items": []}

    # 候选：转发监控 + 未清理过 + （end_time 过期 或 无 end_time 待探测）
    cand = (db.query(models.Activity)
            .filter(models.Activity.source_type == "repost",
                    models.Activity.cleanup_deleted_at.is_(None))
            .order_by(models.Activity.id.desc())
            .limit(200).all())

    # 探测客户端：第一个 active 账号的登录态（拉中奖名单/删除需要登录）
    acc = (db.query(models.Account)
           .filter_by(status="active").order_by(models.Account.id.asc()).first())
    if not acc or not acc.cookies:
        return {**stat, "error": "无 active 账号（清理需要登录态）"}
    client = bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    uid = str(acc.uid)

    for act in cand:
        # 统一探测 lottery_notice（官方抽奖判定 + 中奖名单来源）
        notice = client.get_lottery_notice(act.activity_id)
        notice_ended = _notice_ended(notice) if notice else False
        winners = _parse_winners(notice) if notice else set()
        # 中奖 → 永远保留（两条规则都不删）
        if uid in winners:
            stat["won_keep"] += 1
            continue
        # 规则B（OR）：官方抽奖（有 notice）已开奖 且 未中奖 → 直接删，不等天数
        if notice and notice_ended:
            stat["checked"] += 1
        # 规则A（OR）：开奖距今超过 N 天（cleanup_end_days）且 已开奖 且 未中奖 → 删
        else:
            days_ok = _open_days_ago(db, act, notice, sm)
            ended = bool(act.end_time and act.end_time <= now)
            if not (ended and notice and days_ok is True):
                stat["not_ended"] += 1
                continue
            stat["checked"] += 1
        # 未中奖 → 删除（先找自己的转发动态 id）
        repost_id = client.find_my_repost_id(uid, act.activity_id)
        if not repost_id:
            # 无我的转发动态：大概率该活动我的账号从未参与过（无转发可删）→ 跳过
            try:
                my_participated = int(acc.id) in {
                    int(x) for x in (act.participated_accounts or "[]")
                    .strip("[]").split(",") if x.strip().isdigit()}
            except Exception:
                my_participated = False
            if my_participated:
                stat["failed"] += 1
                stat["items"].append({
                    "id": act.id, "title": (act.title or "")[:30],
                    "reason": "参与过但未找到我的转发动态"})
            else:
                stat["skipped"] += 1
                stat["items"].append({
                    "id": act.id, "title": (act.title or "")[:30],
                    "reason": "未参与（无转发可删）"})
            continue
        stat["to_delete"] += 1
        stat["items"].append({
            "id": act.id, "title": (act.title or "")[:30],
            "activity_id": act.activity_id, "repost_id": repost_id,
            "reason": "已开奖未中奖"})
        if not dry_run:
            if client.delete_dynamic(repost_id):
                act.cleanup_deleted_at = now
                stat["deleted"] += 1
            else:
                stat["failed"] += 1
    db.commit()
    return stat


def cleanup_account_dynamics(db, account, account_id: int = 0,
                             end_days: int = 7,
                             forward_days: int = 0,
                             whitelist: list | None = None,
                             llm_parse: bool = False,
                             interactive_clean: bool = False,
                             r1: bool = True, r2: bool = False,
                             r3: bool = False, r4: bool = False,
                             dry_run: bool = True,
                             max_pages: int = 30,
                             scan_gap: float = 1.0) -> dict:
    """账号维度清理：扫描账号转发动态，按【勾选的规则 AND 组合】删除。

    规则（勾选的才生效，多选 = 全部满足才删）：
      r1 列表内结束日期超 end_days 天
      r2 转发时间超 forward_days 天（不检查列表/结束日期）
      r3 列表外/无结束日期的动态用 LLM 解析出结束时间超 end_days 天
      r4 官方互动抽奖（lottery_notice 命中）已截止 → 专门检查清除
    白名单（中奖动态 id）优先跳过；删除用扫描到的转发动态 id 直删。
    已截止但未满检查期的动态单独分类展示（ended_recent，不删）。
    """
    stat = {"checked": 0, "whitelisted": 0, "not_ended": 0, "no_llm": 0,
            "ended_recent": 0, "recent_items": [],
            "to_delete": 0, "deleted": 0, "failed": 0, "items": []}
    if not account or not account.cookies:
        return {**stat, "error": "账号无登录态"}
    rules = [n for n, on in (("r1", r1), ("r2", r2), ("r3", r3), ("r4", r4)) if on]
    if not rules:
        return {**stat, "error": "请至少勾选一条清理规则"}
    client = bili_client.BiliClient(bili_client.cookies_from_json(account.cookies))
    uid = str(account.uid)
    now = datetime.now()
    if end_days is None:
        end_days = 7
    wl = {str(x).strip() for x in (whitelist or []) if str(x).strip()}

    # 活动表索引（repost 转发记录）
    act_by_orig = {str(a.activity_id): a for a in db.query(models.Activity)
                   .filter(models.Activity.source_type == "repost").all()}

    # 扫描账号空间转发（含翻页间隔防风控）
    _set_progress(account_id, stage="扫描账号动态中...", pages=0)
    forwards = client.scan_my_forwards(
        uid, max_pages=max_pages, gap=scan_gap,
        on_page=lambda n: _set_progress(account_id, stage=f"扫描账号动态中...（已翻 {n} 页）", pages=n))
    _set_progress(account_id, stage="匹配规则中...", forwards=len(forwards))

    # notice 缓存（r4 / 展示用）；探测失败（None）重试 1 次，减少误分类
    notice_cache = {}

    def _notice(orig):
        if orig not in notice_cache:
            n = client.get_lottery_notice(orig)
            if n is None:
                n = client.get_lottery_notice(orig)   # 重试一次（网络/风控抖动）
            notice_cache[orig] = n
        return notice_cache[orig]

    # r3 需要时：先批量 LLM 解析列表外/无结束日期的转发（限 50 条）
    llm_end = {}          # orig -> end_dt
    llm_available = False
    if r3:
        from . import llm_client
        sm = _settings_map(db)
        llm_available = str(sm.get("llm_enabled", "")).lower() in ("true", "1", "yes")
        need = [fw for fw in forwards
                if fw.get("orig_id") and not (act_by_orig.get(fw["orig_id"]) and
                                              act_by_orig[fw["orig_id"]].end_time)]
        if llm_available and need:
            cfg = {"base_url": sm.get("llm_base_url", ""),
                   "api_key": sm.get("llm_api_key", ""),
                   "model": sm.get("llm_model", "")}
            texts = []
            for it in need[:50]:
                _set_progress(account_id, stage=f"LLM 解析中...（{len(texts)}/{min(len(need), 50)}）")
                detail = client.get_dynamic_detail(it["orig_id"])
                text = client._extract_detail_text(detail) if detail else ""
                if len(text.strip()) < 5:
                    text = client._fetch_html_dynamic_text(it["orig_id"])
                texts.append({"id": it["orig_id"], "text": (text or "")[:2000]})
            try:
                res = llm_client.parse_lottery_activities_batch(
                    cfg["base_url"], cfg["api_key"], cfg["model"],
                    texts, batch_size=5,
                    temperature=llm_client.resolve_model_overrides(sm, cfg["model"]).get("temperature"),
                    top_p=llm_client.resolve_model_overrides(sm, cfg["model"]).get("top_p"),
                    max_tokens=llm_client.resolve_model_overrides(sm, cfg["model"]).get("max_tokens"))
            except Exception:
                res = [None] * len(texts)
            for it, verdict in zip(need[:50], res):
                if not verdict or not verdict.get("is_lottery"):
                    llm_end[it["orig_id"]] = None
                    continue
                end_dt = None
                try:
                    et = str(verdict.get("end_time") or "").strip()
                    if et:
                        end_dt = datetime.strptime(et[:16], "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    end_dt = None
                llm_end[it["orig_id"]] = end_dt
        else:
            stat["no_llm"] += len(need)

    # 主循环：AND 判定
    candidates = []
    for idx, fw in enumerate(forwards):
        if idx and idx % 50 == 0:
            _set_progress(account_id, stage=f"匹配规则中...（{idx}/{len(forwards)}）")
        orig = str(fw.get("orig_id") or "")
        if not orig:
            continue
        if orig in wl:
            stat["whitelisted"] += 1
            continue
        act = act_by_orig.get(orig)
        conds = {}
        # r1：列表内结束日期超 N 天
        if "r1" in rules:
            conds["r1"] = bool(act and act.end_time and
                               (now - act.end_time >= timedelta(days=max(end_days, 0))))
        # r2：转发时间超 X 天
        if "r2" in rules:
            ok = False
            if forward_days > 0 and fw.get("pub_ts"):
                try:
                    ok = now - datetime.fromtimestamp(int(fw["pub_ts"])) \
                        >= timedelta(days=forward_days)
                except (TypeError, ValueError, OSError):
                    ok = False
            conds["r2"] = ok
        # r3：LLM 解析结束超 N 天（列表内用 end_time，列表外用 LLM 结果）
        if "r3" in rules:
            if act and act.end_time:
                conds["r3"] = bool(now - act.end_time >= timedelta(days=max(end_days, 0)))
            else:
                end_dt = llm_end.get(orig)
                conds["r3"] = bool(end_dt and now - end_dt >= timedelta(days=max(end_days, 0)))
                if "r3" in conds and llm_end.get(orig) is None:
                    stat["no_llm"] += 1
        # r4：官方互动抽奖已开奖（有中奖名单）且名单无当前账号 → 删（不看时间）
        if "r4" in rules:
            n = _notice(orig)
            if n and n.get("lottery_result"):
                winners = _parse_winners(n)
                conds["r4"] = str(uid) not in winners
            else:
                conds["r4"] = False

        if all(conds.values()):
            # AND 全部满足 → 候选
            labels = []
            if "r1" in rules and conds["r1"]:
                labels.append(f"结束超{end_days}天")
            if "r2" in rules and conds["r2"]:
                labels.append(f"转发超{forward_days}天")
            if "r3" in rules and conds["r3"]:
                labels.append("LLM解析结束超%d天" % end_days)
            if "r4" in rules and conds["r4"]:
                labels.append("官方互动抽奖已截止")
            candidates.append({
                "fw": fw, "act": act, "reason": "+".join(labels)})
        else:
            # 未满足 AND：信息分类展示
            n = _notice(orig)
            # 互动抽奖已开奖（有名单）但未进候选：中奖保留 或 未勾规则4 → 不显示"检查期"
            if n and n.get("lottery_result"):
                stat["not_ended"] += 1
                continue
            # 非互动抽奖已截止但未满检查期（规则1 的检查窗口）→ 单列展示
            end_dt = None
            if act and act.end_time:
                end_dt = act.end_time
            elif n:
                end_dt = bili_client.BiliClient.notice_end_time(n)
            if end_dt and end_dt <= now:
                stat["ended_recent"] += 1
                stat["recent_items"].append({
                    "orig_id": orig, "repost_id": fw["repost_id"],
                    "end_time": end_dt.strftime("%Y-%m-%d %H:%M"),
                    "reason": f"已截止（非互动抽奖，按规则1需满 {end_days} 天）"})
            else:
                stat["not_ended"] += 1
    _set_progress(account_id, candidates=len(candidates),
                  stage=f"匹配完成，候选 {len(candidates)} 条")

    # 统计 / 删除
    total_c = len(candidates)
    for idx, c in enumerate(candidates):
        stat["checked"] += 1
        stat["to_delete"] += 1
        act = c["act"]
        stat["items"].append({
            "id": act.id if act else None,
            "title": (act.title or "")[:30] if act else "",
            "orig_id": c["fw"]["orig_id"],
            "repost_id": c["fw"]["repost_id"],
            "end_time": act.end_time.strftime("%Y-%m-%d %H:%M") if act and act.end_time else "",
            "reason": c["reason"]})
        if not dry_run:
            _set_progress(account_id, stage=f"删除中...（{idx + 1}/{total_c}）",
                          deleted=stat["deleted"], failed=stat["failed"])
            if client.delete_dynamic(c["fw"]["repost_id"]):
                stat["deleted"] += 1
                if act:
                    act.cleanup_deleted_at = now
            else:
                stat["failed"] += 1
    db.commit()
    _set_progress(account_id, deleted=stat["deleted"], failed=stat["failed"])
    return stat


def delete_cleanup_items(db, account, account_id: int = 0,
                         items: list | None = None,
                         dry_run: bool = False) -> dict:
    """直接删除指定转发动态列表（复用统计结果，不重新扫描，删除立即开始）。

    items: [{id, repost_id, orig_id, ...}] —— 来自统计（dry_run）的 result.items。
    返回统计 dict（与 cleanup_account_dynamics 同构）。
    """
    stat = {"checked": 0, "whitelisted": 0, "not_ended": 0, "no_llm": 0,
            "ended_recent": 0, "recent_items": [],
            "to_delete": 0, "deleted": 0, "failed": 0, "items": []}
    if not account or not account.cookies:
        return {**stat, "error": "账号无登录态"}
    items = [it for it in (items or []) if it.get("repost_id")]
    if not items:
        return {**stat, "error": "没有待删除的转发动态（请先统计）"}
    client = bili_client.BiliClient(bili_client.cookies_from_json(account.cookies))
    now = datetime.now()
    stat["to_delete"] = len(items)
    stat["items"] = items
    for idx, it in enumerate(items):
        stat["checked"] += 1
        if not dry_run:
            _set_progress(account_id, stage=f"删除中...（{idx + 1}/{len(items)}）",
                          deleted=stat["deleted"], failed=stat["failed"])
            if client.delete_dynamic(str(it["repost_id"])):
                stat["deleted"] += 1
                if it.get("id"):
                    act = db.get(models.Activity, it["id"])
                    if act:
                        act.cleanup_deleted_at = now
            else:
                stat["failed"] += 1
    db.commit()
    _set_progress(account_id, stage="完成", deleted=stat["deleted"], failed=stat["failed"])
    return stat
