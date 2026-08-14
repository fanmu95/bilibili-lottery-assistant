"""全自动模式：扫描 + 参与 全自动化循环（防风控）

流程（对齐 bilibinggo 自动调度思路 + 用户需求）：
1. 检查当前待参与（pending 且未过期）活动数量
2. 若 >= AUTO_MIN_ACTIVITIES(10)：不扫描，只把剩余活动逐个真实三连参与（限制每轮数量）
3. 若 < 10：扫描下一个未扫描的监控用户（单用户，不批量），入库后立即参与新增活动
4. 循环直到手动停止；每轮间隔防风控

要点：
- 只扫描一个用户/轮（避免短时间内大量请求触发风控）
- 每轮参与数量有限制（默认 5 条），动作间隔 1.2s
- 状态暴露给前端轮询（auto 进度面板）
"""
import threading
import time
from datetime import datetime

from ..database import SessionLocal
from .. import models

AUTO_MIN_ACTIVITIES = 10    # 剩余活动 >= 10 不扫描，只参与
AUTO_MIN_CRITICAL = 3       # 剩余活动 < 3（快耗尽）时忽略冷却强制扫描
AUTO_SCAN_USERS_PER_ROUND = 5  # 活动极少时每轮连续扫描的监控用户数上限（有收获即停）
AUTO_ROUND_SLEEP = 60       # 每轮循环间隔（秒，可被设置 auto_round_sleep 覆盖）
AUTO_PARTICIPATE_PER_ROUND = 5  # 每轮最多参与数量
ACTION_GAP = 1.2            # 动作间隔（防风控，可被设置 activity_gap_min/max 覆盖）
SCAN_COOLDOWN_MIN = 30      # 同一用户扫描冷却（分钟），避免高频扫同一人触发风控


def _settings_map(db) -> dict:
    """读取全部设置（key -> value）"""
    try:
        return {r.key: r.value for r in db.query(models.Setting).all()}
    except Exception:
        return {}


def _activity_gap(db) -> float:
    """活动间随机间隔（秒）：从设置 activity_gap_min/max 读取，默认 3.0~5.0"""
    import random as _random
    try:
        smap = _settings_map(db)
        lo = max(0.5, float(smap.get("activity_gap_min", 3.0)))
        hi = max(lo, float(smap.get("activity_gap_max", 5.0)))
        return _random.uniform(lo, hi)
    except Exception:
        return _random.uniform(3.0, 5.0)


def _parse_accs(raw: str) -> list:
    """解析 participated_accounts JSON，非法返回空列表"""
    import json as _json
    if not raw:
        return []
    try:
        val = _json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


class AutoManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._scheduler_thread = None
        self._stop = False
        self._last_schedule_date = None   # 当天已定时启动的日期（防重复）
        self._last_dm_check = None        # 上次私信检测时间（按 dm_check_interval_min 间隔）
        self._last_review_check = None    # 上次后台复核巡检时间（独立于全自动轮次）
        self._last_auto_scan = None       # 上次定时自动扫描时间（按 scan_interval 间隔）
        self.state = {
            "running": False,
            "message": "未启动",
            "round": 0,
            "participated": 0,      # 本轮累计参与数
            "scanned_user": "",     # 最近扫描的用户
            "pending_count": 0,     # 当前待参与活动数
            "phase": 0,             # 0检查 / 1扫描中 / 2参与中（步骤组件显示）
            "scheduled": False,     # 是否已启用定时启动（auto_schedule_enabled）
            "schedule_time": "",    # 定时启动时间（HH:MM）
            "last_round_at": None,
            "started_at": None,
            "stopped_at": None,
            "next_round_at": "",      # 下一轮开始时间（HH:MM:SS，等待期间）
            "next_round_in": None,    # 距下一轮剩余秒数（等待期间每秒更新，其余为 None）
            # ---- 详细动作展示 ----
            "current_action": "",   # 当前动作（如：点赞/关注/转发/评论/生成文案）
            "current_activity": "", # 当前参与的活动标题
            "current_account": "",  # 当前参与账号
            "action_log": [],       # 最近动作日志 [{ts, text}, ...]（最多 20 条）
        }

    def _set_action(self, text: str, type: str = "info"):
        """记录一条详细动作（加锁，保留最近 20 条）。

        type: info/start/scan/llm/action/success/error —— 前端按类型格式化展示
        """
        with self._lock:
            self.state["current_action"] = text
            log = list(self.state.get("action_log") or [])
            log.append({"ts": datetime.now().strftime("%H:%M:%S"),
                        "text": text, "type": type})
            self.state["action_log"] = log[-20:]
            self.state["message"] = text

    def _update_last_action(self, text: str, type: str = "action"):
        """更新最后一条详细动作（不新增）——用于把点赞/关注/转发/评论
        四步三连进度累积合并成一行显示。列表为空时退化为追加。"""
        with self._lock:
            self.state["current_action"] = text
            log = list(self.state.get("action_log") or [])
            if log:
                log[-1] = {"ts": datetime.now().strftime("%H:%M:%S"),
                           "text": text, "type": type}
            else:
                log.append({"ts": datetime.now().strftime("%H:%M:%S"),
                            "text": text, "type": type})
            self.state["action_log"] = log[-20:]
            self.state["message"] = text

    @property
    def progress(self) -> dict:
        with self._lock:
            return dict(self.state)

    def start(self):
        with self._lock:
            if self.state["running"]:
                return False, "全自动模式已运行中"
            self._stop = False
            self.state.update(
                running=True, message="启动中...", round=0, participated=0,
                scanned_user="", pending_count=0, last_round_at=None, phase=0,
                next_round_at="", next_round_in=None,
                started_at=datetime.now().strftime("%H:%M:%S"), stopped_at=None)
        # 启动前先清理已结束的待参与活动（end_time 过期 / 无 end_time 发布超时
        # 的标记 ended），避免启动后第一轮就参与早已结束的活动
        try:
            from ..routers.logs import add_log
            db = SessionLocal()
            try:
                expired = self._mark_expired(db)
                if expired:
                    add_log(db, "info", "auto",
                            f"启动前清理 {expired} 个已结束活动（标记 ended）")
            finally:
                db.close()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._ensure_scheduler()
        return True, "全自动模式已启动"

    def stop(self):
        with self._lock:
            self._stop = True
            self.state["message"] = "停止中..."
        return True, "已请求停止"

    # ---------- 定时自动启动（auto_schedule_enabled / auto_schedule_time） ----------

    def _ensure_scheduler(self):
        """确保定时调度线程运行（单例 daemon，每 30s 检查一次）"""
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            return
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def _auto_read_dm_replies(self, db) -> int:
        """私信检测：对全部 active 账号的「自动回复」会话自动已读（msg_source=8）。

        按设置 dm_check_interval_min 间隔由调度循环调用，独立于前端轮询；
        只清未读标记、不隐藏消息。返回自动已读的会话总数。
        """
        from . import bili_client
        try:
            from ..routers.accounts import _auto_read_auto_reply
        except Exception:
            return 0
        total = 0
        for acc in db.query(models.Account).filter_by(status="active").all():
            try:
                client = bili_client.BiliClient(
                    bili_client.cookies_from_json(acc.cookies))
                sessions = client.get_sessions()
                total += _auto_read_auto_reply(client, sessions)
            except Exception:
                continue
        return total

    def _scheduler_loop(self):
        """常驻调度：①到设定时间自动启动全自动（每天最多一次）；
        ②私信检测——按设置间隔（dm_check_interval_min）自动已读自动回复私信；
        ③后台复核——独立于全自动轮次，按 review_interval_min 间隔自动
        复核纠错（奖品/结束时间补齐），不开全自动也持续修正。"""
        from ..routers.logs import add_log
        while True:
            try:
                db = SessionLocal()
                try:
                    settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
                    # ---- ① 定时自动启动全自动 ----
                    enabled = str(settings_map.get("auto_schedule_enabled", "")).lower() in ("true", "1", "yes")
                    sched_time = str(settings_map.get("auto_schedule_time", "10:00")).strip()
                    with self._lock:
                        self.state["scheduled"] = enabled
                        self.state["schedule_time"] = sched_time
                    if enabled and sched_time:
                        now = datetime.now()
                        today = now.date().isoformat()
                        # 到点 + 今天没启动过 + 未在运行 -> 自动启动
                        if (now.strftime("%H:%M") == sched_time
                                and self._last_schedule_date != today
                                and not self.state["running"]):
                            ok, msg = self.start()
                            self._last_schedule_date = today
                            add_log(db, "info", "auto",
                                    f"定时启动全自动（{sched_time}）：{msg}")
                    # ---- ② 私信检测：自动已读自动回复私信（按设置间隔 + 时间窗白名单）----
                    try:
                        interval = int(float(settings_map.get("dm_check_interval_min", 30)))
                        in_window = True
                        try:
                            st = settings_map.get("dm_check_start", "08:00").strip()
                            en = settings_map.get("dm_check_end", "22:00").strip()
                            st_h, st_m = map(int, st.split(":"))
                            en_h, en_m = map(int, en.split(":"))
                            cur = datetime.now()
                            cur_m = cur.hour * 60 + cur.minute
                            in_window = (st_h * 60 + st_m) <= cur_m <= (en_h * 60 + en_m)
                        except Exception:
                            pass
                        now = datetime.now()
                        due = (self._last_dm_check is None
                               or (now - self._last_dm_check).total_seconds() >= interval * 60)
                        if in_window and due:
                            self._last_dm_check = now
                            n = self._auto_read_dm_replies(db)
                            if n:
                                add_log(db, "info", "account",
                                        f"私信检测：自动已读 {n} 个自动回复会话")
                    except Exception:
                        pass
                    # ---- ③ 后台复核：独立于全自动轮次，定时修正奖品/结束时间 ----
                    # 首次巡检立即触发（清积压），之后按 review_interval_min 间隔；
                    # 与全自动轮次的触发共用单例锁，不会并发重复。
                    try:
                        rv_interval = int(float(settings_map.get(
                            "review_interval_min", 5)))
                        if rv_interval < 1:
                            rv_interval = 1
                        now = datetime.now()
                        rv_due = (self._last_review_check is None
                                  or (now - self._last_review_check).total_seconds()
                                  >= rv_interval * 60)
                        if rv_due:
                            self._last_review_check = now
                            from .review_service import trigger_review_thread
                            trigger_review_thread()
                    except Exception:
                        pass
                    # ---- ④ 定时自动扫描（scan_interval 分钟间隔，独立于全自动）----
                    # 后端常驻按间隔批量扫描监控用户补货；受活动发现页"自动扫描"
                    # 开关（auto_scan_enabled）控制，扫描未运行时才触发
                    try:
                        if str(settings_map.get("auto_scan_enabled", "true")).lower() \
                                in ("true", "1", "yes"):
                            sc_interval = int(float(settings_map.get("scan_interval", 60)))
                            if sc_interval < 5:
                                sc_interval = 5
                            now = datetime.now()
                            sc_due = (self._last_auto_scan is None
                                      or (now - self._last_auto_scan).total_seconds()
                                      >= sc_interval * 60)
                            if sc_due:
                                self._last_auto_scan = now
                                from .scan_service import scan_manager
                                if not scan_manager.progress.get("running"):
                                    ok, _msg = scan_manager.start()
                                    if ok:
                                        add_log(db, "info", "scan",
                                                f"定时自动扫描启动（间隔 {sc_interval} 分钟）")
                    except Exception:
                        pass
                finally:
                    db.close()
            except Exception:
                pass
            time.sleep(30)

    # ------------------------------------------------------------------

    def _count_pending(self, db) -> int:
        """待参与（pending 且未过期）活动数。

        无 end_time（结束时间未定）的活动同样视为可参与——
        只有明确已过期的才排除。
        """
        from .bili_client import BiliClient  # noqa
        now = datetime.now()
        return db.query(models.Activity).filter(
            models.Activity.status == "pending",
            (models.Activity.end_time.is_(None))
            | (models.Activity.end_time > now),
        ).count()

    def _quota_exhausted(self, db) -> bool:
        """今日参与配额是否已用完（daily_participate_limit=0 表示不限）。

        配额满时主循环进入「仅扫描」模式：不参与但持续扫描补充活动，
        0 点后配额自动重置，参与自动恢复——避免空转"无可参与"。
        """
        try:
            daily_limit = int(float(_settings_map(db).get(
                "daily_participate_limit", 100)))
        except (TypeError, ValueError):
            daily_limit = 100
        if daily_limit <= 0:
            return False
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        today_cnt = (db.query(models.Activity)
                     .filter(models.Activity.participated_at >= today_start)
                     .count())
        return today_cnt >= daily_limit

    def _mark_expired(self, db) -> int:
        """把已明确结束的待参与活动标记 ended：
        仅基于「end_time 已过期」（确定无疑）。
        注意：无 end_time 的活动**不**按发布时长自动 ended——
        长周期抽奖/未写时间的活动可能仍在进行，误杀成本远高于多参与一次。
        （普通抽奖已结束靠参与前 notice 探测兜底；互动抽奖有官方开奖时间）
        """
        now = datetime.now()
        expired = (db.query(models.Activity)
                   .filter(models.Activity.status == "pending",
                           models.Activity.end_time.isnot(None),
                           models.Activity.end_time <= now)
                   .all())
        for a in expired:
            a.status = "ended"
        cnt = len(expired)
        if cnt:
            db.commit()
        return cnt

    def _next_scan_user(self, db, force: bool = False,
                        cooldown_seconds: int | None = None):
        """找下一个可扫描的监控用户（轮询均衡 + 冷却保护）。

        冷却默认 SCAN_COOLDOWN_MIN 分钟；cooldown_seconds 可覆盖——
        配额满"仅扫描"模式时传轮次间隔（auto_round_sleep），
        让每轮都能扫到用户持续补货，同一用户至少隔一个轮次再扫。
        force=True（待参与活动极少时）：忽略冷却直接扫描，
        否则活动耗尽后一直等冷却会完全无活动可参与。
        """
        from datetime import timedelta
        cooldown = cooldown_seconds if cooldown_seconds else SCAN_COOLDOWN_MIN * 60
        cooldown_before = datetime.now() - timedelta(seconds=cooldown)
        users = (db.query(models.MonitorUser)
                 .filter(models.MonitorUser.status == "active")
                 .order_by(models.MonitorUser.last_scanned_at.is_(None),
                           models.MonitorUser.last_scanned_at.asc())
                 .all())
        for u in users:
            if force or u.last_scanned_at is None or u.last_scanned_at < cooldown_before:
                return u
        return None

    def _scan_one_user(self, db, user) -> int:
        """扫描单个监控用户并入库，返回新增活动数"""
        from ..routers.logs import add_log
        from .scan_service import scan_single_user
        try:
            found = scan_single_user(db, user)
            return found
        except Exception as e:
            add_log(db, "error", "scan", f"全自动扫描 {user.username} 失败: {e}")
            return 0

    def _participate_pending(self, db, limit: int) -> int:
        """对剩余待参与活动逐个真实三连参与（最多 limit 个）。

        多账号：所有 active 账号轮流参与（每个活动优先未参与过的账号），
        避免固定只用第一个账号。
        """
        from ..routers.logs import add_log
        from ..routers.activities import _default_account
        from . import bili_actions, bili_client
        from .participate_text_service import resolve_participate_text
        from datetime import timedelta

        # 所有已登录账号（轮流参与）
        accounts_all = (db.query(models.Account)
                        .filter_by(status="active")
                        .order_by(models.Account.id.asc()).all())
        if not accounts_all:
            add_log(db, "warning", "auto", "全自动参与失败：无已登录账号")
            return 0
        account = accounts_all[0]   # 主账号（用于配额等）

        # 匿名探测 client（本轮共享）：对无 end_time 的活动参与前查
        # 是否已开奖结束，避免活动早结束还在参与（每轮构造 1 次，warmup 1 次）
        probe_client = None
        try:
            probe_client = bili_client.BiliClient()
        except Exception:
            probe_client = None

        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}

        # 评论池补齐：待参与活动评论池不足账号数时批量生成（后台线程，不阻塞参与/停止）
        try:
            if (str(settings_map.get("participate_text_mode", "custom")) in ("llm_generate", "random")
                    and settings_map.get("llm_base_url") and settings_map.get("llm_model")):
                def _gen_pools_bg():
                    from .participate_text_service import ensure_comment_pools
                    s = SessionLocal()
                    try:
                        n = ensure_comment_pools(s, limit=15)
                        if n:
                            self._set_action(
                                f"补齐 {n} 个活动的评论池（每活动按账号数）", "llm")
                    except Exception:
                        s.rollback()
                    finally:
                        s.close()
                threading.Thread(target=_gen_pools_bg, daemon=True).start()
        except Exception:
            pass

        # 每日参与配额校验（防风控：超限停止，避免账号被标记）
        try:
            daily_limit = int(float(settings_map.get("daily_participate_limit", 100)))
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_cnt = (db.query(models.Activity)
                         .filter(models.Activity.participated_at >= today_start)
                         .count())
            if daily_limit > 0 and today_cnt >= daily_limit:
                add_log(db, "warning", "auto",
                        f"今日参与已达上限 {daily_limit} 个，全自动暂停参与")
                return 0
        except Exception:
            pass   # 配额读取失败不阻塞参与

        mode = settings_map.get("participate_text_mode", "custom")
        custom_text = settings_map.get("participate_text", "")
        llm_cfg = {
            "base_url": settings_map.get("llm_base_url", ""),
            "api_key": settings_map.get("llm_api_key", ""),
            "model": settings_map.get("llm_model", ""),
        } if mode in ("llm_generate", "random") else None

        now = datetime.now()
        # 多账号：活动被其他账号参与过（status=participated）仍可参与，
        # 具体账号是否已参与在循环里按 participated_accounts 判断。
        # 重要：候选池要放大并在内存里过滤「所有账号都已参与」的活动——
        # 否则按 end_time 排序取前几条全是参与完的，每轮空转（participated=0）。
        import json as _json
        active_ids = [a.id for a in db.query(models.Account)
                      .filter_by(status="active").all()]
        rows = (db.query(models.Activity)
                .filter(models.Activity.status.in_(["pending", "participated"]),
                        (models.Activity.end_time.is_(None))
                        | (models.Activity.end_time > now))
                # 未参与过的活动排最前（新活动 end_time 常很晚，
                # 若按 end_time asc + limit 会把新活动切掉导致「无可参与」）
                # 注意：SQLite 中 NULL 在 ASC 排序里排**最前**——end_time 为空的
                # 活动会被优先参与，导致不按最近开奖日期执行。必须加
                # end_time.is_(None).asc() 让无日期活动排最后。
                .order_by(models.Activity.participated_at.is_(None).desc(),
                          models.Activity.end_time.is_(None).asc(),
                          models.Activity.end_time.asc())
                .limit(limit * 15).all())
        # 内存过滤：排除所有 active 账号都已参与的活动（无账号可参与）
        rows = [a for a in rows
                if not (active_ids and all(
                    aid in (_parse_accs(a.participated_accounts) or [])
                    for aid in active_ids))][:limit * 6]
        # 参与顺序优化：优先「部分参与」的活动（补全后变 participated，待参与数减少），
        # 再参与全新活动——否则每个活动只被一个账号参与后仍 pending，数量看起来不变。
        rows = sorted(rows, key=lambda a: len(_parse_accs(a.participated_accounts) or []) == 0)

        participated = 0
        for act in rows:
            if self._stop or participated >= limit:
                break
            # 跳过正被参与队列处理的活动（防并发写 participated_accounts 竞态）
            from .participation_service import is_activity_busy
            if is_activity_busy(act.id):
                continue
            act_accounts = []
            try:
                act_accounts = _json.loads(act.participated_accounts or "[]")
                if not isinstance(act_accounts, list):
                    act_accounts = []
            except Exception:
                act_accounts = []
            # 参与该活动所有未参与过的账号（一轮参与完，避免半参与积压）：
            # 之前每轮只参与一个账号 → 半参与活动要等下一轮（轮次间隔可长达
            # 1000s）才补全，participated_at（今日参与计数依据）延迟设置，
            # 账号"今日参与"统计长时间不更新
            pending_accounts = [a for a in accounts_all if a.id not in act_accounts]
            if not pending_accounts:
                continue   # 所有账号都已参与过
            for account in pending_accounts:
                # 该账号的登录 client（每次参与用对应账号身份）
                act_client = None
                try:
                    if account.cookies:
                        act_client = bili_client.BiliClient(
                            bili_client.cookies_from_json(account.cookies))
                except Exception:
                    act_client = None
                # 详细动作：当前活动/账号
                act_title = (act.title or "")[:30].replace("\n", " ")
                with self._lock:
                    self.state["current_activity"] = act_title
                    self.state["current_account"] = account.username
                # ---- 已结束校验：无 end_time 的活动参与前实时探测 ----
                # 仅依据官方数据：互动抽奖 lottery_notice 显示已开奖（有中奖名单/status 非进行中）。
                # 普通抽奖（无 notice、无 end_time）不按发布时长判死——
                # 长周期/未写时间的活动可能仍在进行，宁可多参与一次。
                if act.end_time is None:
                    ended = False
                    notice = None
                    if probe_client is not None:
                        try:
                            notice = probe_client.get_lottery_notice(act.activity_id)
                            ended = bili_client.BiliClient.notice_is_ended(notice)
                        except Exception:
                            notice = None
                    if ended:
                        act.status = "ended"
                        self._set_action(
                            f"活动已开奖（官方），跳过参与「{act_title}」", "info")
                        add_log(db, "info", "auto",
                                f"全自动跳过已开奖活动：{act_title}")
                        break
                # ---- 充电抽奖跳过（设置 skip_charge_lottery）----
                # 充电抽奖需要付费充电才能参与，自动跳过；识别标题/正文含明确充电抽奖字样
                try:
                    _skip_charge = str(settings_map.get(
                        "skip_charge_lottery", "true")).lower() in ("true", "1", "yes")
                except Exception:
                    _skip_charge = True
                if _skip_charge:
                    _txt = ((act.title or "") or "") + " " + ((act.desc or "") or "")
                    if "充电抽" in _txt or "充电抽奖" in _txt:
                        act.status = "skipped"
                        self._set_action(
                            f"充电抽奖，自动跳过「{act_title}」", "info")
                        add_log(db, "info", "auto",
                                f"全自动跳过充电抽奖：{act_title}")
                        break
                self._set_action(f"开始参与「{act_title}」（{account.username}）", "start")
                # 参与文案
                # 评论取用：优先用预生成评论池（按账号取不同，秒用不等 LLM）；
                # 只有池为空才现场生成（LLM/随机/自定义），LLM 失败才 fallback 兜底
                from .participate_text_service import pick_comment_for_account
                comment_text = pick_comment_for_account(act, account.id) or ""
                if not comment_text:
                    self._set_action(f"LLM 生成「{act_title}」的参与文案...", "llm")
                    res = resolve_participate_text(
                        mode=mode, custom_text=custom_text,
                        fallback_text="关注+转发，支持一下，谢谢！",
                        client=act_client, dynamic_id=act.activity_id,
                        activity_text=(act.desc or "") or act.title or "",
                        llm_cfg=llm_cfg,
                        allow_network=mode in ("random_comment", "llm_generate", "random"))
                    comment_text = res["text"]
                    if not act.comment_text and mode in ("random_comment", "llm_generate", "random"):
                        act.comment_text = comment_text
                # 真实三连
                errors = []
                if act_client is not None:
                    try:
                        detail = act_client.get_dynamic_detail(act.activity_id)
                        rid, ctype = "", 17
                        if detail:
                            rid, ctype = bili_actions.extract_comment_oid(detail)
                        steps = ("like", "repost", "comment")
                        if act.author_uid:
                            steps = ("like", "follow", "repost", "comment")

                        step_parts = []

                        def on_step(step_index, total, action, detail):
                            # 三连动作合并成一行显示：第一步新增一条，后续步骤更新同一条
                            # 最终形如：三连：点赞（1/4）→ 关注（2/4）→ 转发（3/4）→ 评论（4/4）
                            step_parts.append(detail)
                            text = "三连：" + " → ".join(step_parts)
                            if step_index == 1:
                                self._set_action(text, "action")
                            else:
                                self._update_last_action(text, "action")

                        exec_res = bili_actions.execute_participation(
                            act_client, dynamic_id=act.activity_id,
                            sender_uid=act.author_uid or "", comment_text=comment_text,
                            comment_rid=rid, comment_type=ctype, steps=steps,
                            on_step=on_step)
                        errors = exec_res.get("errors", [])
                    except Exception as e:
                        errors.append(f"互动异常: {e}")
                else:
                    errors.append("无 cookies，仅本地记录")
                act_accounts.append(account.id)
                act.participated_accounts = __import__("json").dumps(act_accounts)
                # 状态语义：所有 active 账号都参与过才置 participated，否则保持 pending
                active_ids = [a.id for a in db.query(models.Account)
                              .filter_by(status="active").all()]
                if active_ids and all(aid in act_accounts for aid in active_ids):
                    if act.status != "participated":
                        act.status = "participated"
                        act.participated_at = datetime.now()
                else:
                    if act.status == "participated":
                        act.status = "pending"
                participated += 1
                summary = "成功" if not errors else "; ".join(errors[:2])
                self._set_action(f"完成「{act_title}」（{account.username}）：{summary}",
                                 "success" if not errors else "error")
                # 今日已参与活动数（participated_at 在今天 0 点之后的完成活动）
                today_start = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0)
                today_cnt = db.query(models.Activity).filter(
                    models.Activity.participated_at >= today_start).count()
                add_log(db, "success", "auto",
                        f"全自动参与 {act.title}（{account.username}）：{summary}"
                        f" | 今日已参与 {today_cnt} 个活动")
                if not self._stop:
                    gap = _activity_gap(db)
                    waited = 0
                    while waited < gap and not self._stop:
                        time.sleep(0.5)
                        waited += 0.5
        db.commit()
        return participated

    def _loop(self):
        from ..routers.logs import add_log
        db = SessionLocal()
        try:
            add_log(db, "info", "auto", "全自动模式启动")
            with self._lock:
                self.state["message"] = "运行中"
            while not self._stop:
                # 每轮先清理已结束活动（end_time 过期 / 无 end_time 发布超时），
                # 避免表格里堆着早已开奖的活动
                try:
                    expired_cnt = self._mark_expired(db)
                    if expired_cnt:
                        add_log(db, "info", "auto",
                                f"清理 {expired_cnt} 个已结束活动（标记 ended）")
                except Exception:
                    pass
                pending = self._count_pending(db)
                with self._lock:
                    self.state["pending_count"] = pending
                    self.state["round"] += 1
                    self.state["last_round_at"] = datetime.now().strftime("%H:%M:%S")
                    self.state["message"] = (f"第 {self.state['round']} 轮："
                                             f"待参与 {pending} 个")
                # 今日配额已满：不参与、不扫描（活动发现页"自动扫描"开关负责补货），
                # 等待 0 点后配额重置自动恢复参与
                quota_exhausted = self._quota_exhausted(db)
                if quota_exhausted:
                    add_log(db, "warning", "auto",
                            "今日参与配额已用完，暂停参与，等待 0 点后自动恢复参与")
                    pending = 0   # 仅用于分支判断（state 已保存真实值）

                if pending < AUTO_MIN_ACTIVITIES:
                    if quota_exhausted:
                        # 配额已满：不扫描不参与（取消"配额满自动开启扫描"），等 0 点恢复
                        self._set_action(
                            "今日参与配额已用完，等待 0 点后自动恢复参与", "info")
                    elif pending < AUTO_MIN_CRITICAL:
                        # 活动快耗尽：连续扫描多个监控用户，直到有新活动入库或用户扫完，
                        # 避免每轮只扫 1 个用户且该用户无新转发时一直"扫描=0"看起来像没扫
                        scanned_users = 0
                        total_found = 0
                        while scanned_users < AUTO_SCAN_USERS_PER_ROUND and not self._stop:
                            user = self._next_scan_user(db, force=True)
                            if not user:
                                break
                            with self._lock:
                                self.state["scanned_user"] = user.username
                                self.state["phase"] = 1
                                self.state["message"] = (
                                    f"第 {self.state['round']} 轮：活动不足，"
                                    f"扫描 {user.username}...")
                            self._set_action(f"扫描监控用户 {user.username} 的动态...", "scan")
                            add_log(db, "info", "auto",
                                    f"全自动：待参与 {pending} < {AUTO_MIN_ACTIVITIES}，"
                                    f"扫描 {user.username}")
                            found = self._scan_one_user(db, user)
                            total_found += found
                            scanned_users += 1
                            if found > 0:
                                # 有新活动入库：刷新待参与数（自动扫描入库后界面同步）
                                with self._lock:
                                    self.state["pending_count"] = self._count_pending(db)
                            self._set_action(
                                f"扫描 {user.username} 完成，发现 {found} 个新活动"
                                f"（本轮已扫 {scanned_users} 人）",
                                "success" if found else "info")
                            add_log(db, "info", "auto",
                                    f"扫描 {user.username} 完成，新增 {found} 个活动")
                            if found > 0:
                                break   # 有新活动入库即停，进入参与
                        if scanned_users == 0:
                            with self._lock:
                                self.state["message"] = (
                                    f"第 {self.state['round']} 轮：监控用户均无可扫描")
                            add_log(db, "warning", "auto",
                                    "全自动：没有可扫描的监控用户")
                    else:
                        # 活动略少（3~10）：只扫 1 个用户，遵守冷却防风控
                        user = self._next_scan_user(db)
                        if user:
                            with self._lock:
                                self.state["scanned_user"] = user.username
                                self.state["phase"] = 1
                                self.state["message"] = (
                                    f"第 {self.state['round']} 轮：活动不足"
                                    f" {AUTO_MIN_ACTIVITIES} 个，扫描 {user.username}...")
                            self._set_action(f"扫描监控用户 {user.username} 的动态...", "scan")
                            add_log(db, "info", "auto",
                                    f"全自动：待参与 {pending} < {AUTO_MIN_ACTIVITIES}，"
                                    f"扫描 {user.username}")
                            found = self._scan_one_user(db, user)
                            if found > 0:
                                # 有新活动入库：刷新待参与数
                                with self._lock:
                                    self.state["pending_count"] = self._count_pending(db)
                            self._set_action(
                                f"扫描 {user.username} 完成，发现 {found} 个新活动", "success")
                            add_log(db, "info", "auto",
                                    f"扫描 {user.username} 完成，新增 {found} 个活动")
                        else:
                            with self._lock:
                                self.state["message"] = (
                                    f"第 {self.state['round']} 轮：待参与 {pending} 个，"
                                    f"监控用户均在冷却期"
                                    f"（{SCAN_COOLDOWN_MIN} 分钟内不重复扫描）")
                            add_log(db, "warning", "auto",
                                    "全自动：所有监控用户在冷却期，本轮不扫描")
                else:
                    with self._lock:
                        self.state["message"] = (f"第 {self.state['round']} 轮："
                                                 f"待参与 {pending} 个，直接参与")

                # 参与剩余活动（每轮限流）
                if quota_exhausted:
                    # 配额已满：跳过参与（避免误报"无可参与"），等待 0 点恢复
                    participated = 0
                    self._set_action(
                        "今日参与配额已用完，暂停参与（0 点后自动恢复参与）",
                        "warning")
                else:
                    with self._lock:
                        self.state["phase"] = 2      # 参与中
                    # 每轮参与数量：读设置 participate_batch（默认 5），防风控限流
                    try:
                        per_round = int(float(_settings_map(db).get(
                            "participate_batch", AUTO_PARTICIPATE_PER_ROUND)))
                        if per_round < 1:
                            per_round = 1
                        if per_round > 20:
                            per_round = 20
                    except Exception:
                        per_round = AUTO_PARTICIPATE_PER_ROUND
                    self._set_action(f"第 {self.state['round']} 轮：参与中（待参与 {pending} 个，本轮最多 {per_round} 个）...", "info")
                    participated = self._participate_pending(db, per_round)
                if participated:
                    with self._lock:
                        self.state["participated"] += participated
                    remain = self._count_pending(db)
                    with self._lock:
                        self.state["pending_count"] = remain
                    self._set_action(f"第 {self.state['round']} 轮参与完成，共参与 {participated} 个（剩余待参与 {remain}）", "success")
                    add_log(db, "success", "auto",
                            f"全自动本轮参与 {participated} 个活动")
                else:
                    if quota_exhausted:
                        self._set_action(
                            "今日参与配额已用完，暂停参与（0 点后自动恢复参与）",
                            "warning")
                    else:
                        self._set_action(f"第 {self.state['round']} 轮：无可参与的新活动（待参与 {pending} 个）", "info")
                with self._lock:
                    self.state["phase"] = 0      # 回到检查

                if self._stop:
                    break
                # 每轮参与完成后立即触发异步复核（两阶段解析：第二次 LLM 评判
                # 纠错奖品/结束时间）。放在 continue 之前——参与活跃（剩余<10
                # 立即下一轮）时也会触发，不依赖轮次 sleep 分支。
                try:
                    from .review_service import trigger_review_thread
                    trigger_review_thread()
                except Exception:
                    pass
                # 参与完且待参与仍不足 10 个 -> 跳过轮次等待，立即进入下一轮扫描补活动
                if participated and remain < AUTO_MIN_ACTIVITIES:
                    with self._lock:
                        self.state["next_round_at"] = ""
                        self.state["next_round_in"] = None
                    continue
                # 轮次间隔（可配置：auto_round_sleep，默认 60s）
                try:
                    _round_sleep = int(float(_settings_map(db).get(
                        "auto_round_sleep", AUTO_ROUND_SLEEP)))
                    if _round_sleep < 10:
                        _round_sleep = 10   # 最小 10s，防高频空转
                except Exception:
                    _round_sleep = AUTO_ROUND_SLEEP
                # 轮次冷却期：启动职业号发现（独立线程，与参与/扫描错峰——
                # 只在轮次等待的冷却窗口运行，避免同一时间大量请求 B 站；
                # 开关：设置 auto_pro_scan_enabled）
                try:
                    _sm2 = _settings_map(db)
                    _pro_enabled = str(_sm2.get(
                        "auto_pro_scan_enabled", "True")).lower() in ("true", "1", "yes")
                    if _pro_enabled:
                        from .pro_discovery import (start_discovery,
                                                    get_discovery_progress,
                                                    _pick_candidate_activity)
                        _pro = get_discovery_progress()
                        if not _pro.get("running"):
                            _cand_id = _pick_candidate_activity(db)
                            if _cand_id:
                                ok, _msg = start_discovery(_cand_id)
                                if ok:
                                    add_log(db, "info", "activity",
                                            f"自动模式冷却期：启动职业号发现（活动 {_cand_id}）")
                except Exception:
                    pass
                # 下一轮倒计时（展示用）：等待开始时记录目标时间，每秒更新剩余秒数
                _round_end_ts = datetime.now().timestamp() + _round_sleep
                with self._lock:
                    self.state["next_round_at"] = datetime.fromtimestamp(
                        _round_end_ts).strftime("%H:%M:%S")
                    self.state["next_round_in"] = _round_sleep
                # 可中断等待：每 1s 检查停止标志，点「停止」立即响应（不再傻等整轮）
                waited = 0
                while waited < _round_sleep and not self._stop:
                    time.sleep(1)
                    waited += 1
                    with self._lock:
                        self.state["next_round_in"] = max(
                            0, int(_round_end_ts - datetime.now().timestamp()))
                # 下一轮开始：暂停职业号发现（避免与参与/扫描同时请求 B 站）
                try:
                    from .pro_discovery import stop_discovery
                    stop_discovery()
                except Exception:
                    pass
                with self._lock:
                    self.state["next_round_at"] = ""
                    self.state["next_round_in"] = None
            with self._lock:
                self.state["running"] = False
                self.state["message"] = "已停止"
                self.state["stopped_at"] = datetime.now().strftime("%H:%M:%S")
            add_log(db, "info", "auto", "全自动模式停止")
        except Exception as e:
            add_log(db, "error", "auto", f"全自动模式异常: {e}")
            with self._lock:
                self.state["running"] = False
                self.state["message"] = f"异常: {e}"
        finally:
            db.close()


auto_manager = AutoManager()
