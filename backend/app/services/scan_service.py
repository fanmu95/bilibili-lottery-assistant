"""扫描服务：批量扫描监控用户动态，识别并入库抽奖活动（带进度）

流程：
1. 拉取监控用户转发/发布动态（真实接口，登录态优先）
2. 文本动态：关键词初筛命中 -> LLM 深度解析（提取奖品/开奖时间等）
3. 纯图片动态：下载图片 -> LLM vision 解析（判断是否抽奖）
4. LLM 解析并行执行（ThreadPoolExecutor），429 风控自动重试
5. 解析结果去重入库（activity_id 唯一）
6. 通过内存状态暴露扫描进度供前端轮询
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime

from ..database import SessionLocal
from .. import models
from . import bili_client

# ---- 断点续扫：已完成扫描的用户 uid 集合（持久化到 setting）----
RESUME_SETTING_KEY = "scan_resume_done"


def load_resume_done(db) -> set:
    """读取断点：已完成扫描的用户 uid 集合"""
    try:
        r = db.query(models.Setting).filter_by(key=RESUME_SETTING_KEY).first()
        if not r or not r.value:
            return set()
        return {str(x) for x in json.loads(r.value) if str(x)}
    except Exception:
        return set()


def save_resume_done(db, uids: set):
    """写入断点（已完成用户 uid 集合，实时更新）"""
    try:
        r = db.query(models.Setting).filter_by(key=RESUME_SETTING_KEY).first()
        if not r:
            r = models.Setting(key=RESUME_SETTING_KEY, value="[]")
            db.add(r)
        r.value = json.dumps([str(x) for x in uids])
        db.commit()
    except Exception:
        db.rollback()


def clear_resume_done(db):
    """清空断点（扫描自然完成 / 手动重扫全部）"""
    try:
        r = db.query(models.Setting).filter_by(key=RESUME_SETTING_KEY).first()
        if r:
            r.value = "[]"
            db.commit()
    except Exception:
        db.rollback()

SCAN_SLEEP = 0.6      # 每个用户之间的处理间隔（风控友好）
LLM_WORKERS = 3       # LLM 批量解析并行线程数（批间并行）
LLM_BATCH = 5        # 每批 LLM 请求解析的动态条数（思考模式下批次调小，频率翻倍防截断）
LLM_TIMEOUT = 120     # 单次 LLM 请求超时
PRO_EMPTY_LIMIT = 3   # 职业号连续扫描无活动次数上限（达到则标记失效，防"伪职业号"长期占用）


def _build_client(db) -> bili_client.BiliClient:
    """构建 B 站客户端：优先使用第一个已登录账号的 cookies。

    匿名 session 访问空间动态可能被风控（返回非 JSON），
    带登录态才能稳定拿到真实动态。接口失败时返回空列表（不 mock）。
    """
    acc = (db.query(models.Account)
           .filter(models.Account.status == "active")
           .order_by(models.Account.id.asc()).first())
    if acc and acc.cookies:
        return bili_client.BiliClient(bili_client.cookies_from_json(acc.cookies))
    return bili_client.BiliClient()


class ScanManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._generation = 0   # 线程代际：旧线程收尾时不覆盖新线程状态
        self.state = {
            "running": False,
            "total": 0,
            "done": 0,
            "current_user": "",
            "found": 0,
            "message": "",
            "started_at": None,
            "finished_at": None,
            # LLM 深度解析环节
            "llm_enabled": False,
            "llm_done": 0,
            "llm_total": 0,
            "llm_success": 0,     # 判定为抽奖并入库
            "llm_fail": 0,        # 判定非抽奖
            "llm_image": 0,       # 其中图片动态数
            "llm_current": "",    # 当前解析对象
        }

    @property
    def progress(self) -> dict:
        with self._lock:
            data = dict(self.state)
        # 无扫描任务运行时：llm_enabled 按当前设置实时反映，
        # 避免后端重启后 state 残留初始 False 误导 UI 显示"未启用"
        if not data.get("running"):
            try:
                from ..database import SessionLocal
                from .. import models
                db = SessionLocal()
                try:
                    sm = {r.key: r.value for r in db.query(models.Setting).all()}
                    data["llm_enabled"] = (
                        str(sm.get("llm_enabled", "")).lower() in ("true", "1", "yes")
                        and str(sm.get("scan_llm_verify", "")).lower()
                        in ("true", "1", "yes"))
                finally:
                    db.close()
            except Exception:
                pass
        return data

    def start(self, user_ids=None, reset=False):
        with self._lock:
            if self.state["running"]:
                return False, "扫描已在进行中"
            if self._thread and self._thread.is_alive():
                return False, "上次扫描仍在退出中，请稍候几秒再试"
            if reset:
                db = SessionLocal()
                try:
                    clear_resume_done(db)
                finally:
                    db.close()
            self._stop = False
            self.state.update(
                running=True, total=0, done=0, current_user="", found=0,
                message="准备中...", started_at=datetime.now().strftime("%H:%M:%S"),
                finished_at=None,
                llm_enabled=False, llm_done=0, llm_total=0, llm_success=0,
                llm_fail=0, llm_image=0, llm_current="")
        with self._lock:
            self._generation += 1
            gen = self._generation
        self._thread = threading.Thread(target=self._run, args=(user_ids, gen), daemon=True)
        self._thread.start()
        return True, "扫描已启动"

    def reset_resume(self):
        """清空断点（重扫全部）"""
        db = SessionLocal()
        try:
            clear_resume_done(db)
        finally:
            db.close()

    def stop(self):
        with self._lock:
            self._stop = True
            # 乐观置位：界面立即恢复可操作（后台线程收到 _stop 后尽快退出，
            # 收尾时不再覆盖本状态）
            if self.state["running"]:
                self.state["running"] = False
                self.state["message"] = "扫描已停止"

    # ------------------------------------------------------------------

    def _run(self, user_ids, gen: int = None):
        from ..routers.logs import add_log
        from . import llm_client
        db = SessionLocal()
        try:
            add_log(db, "info", "scan", "扫描任务启动")
            client = _build_client(db)

            # 读取 LLM 配置
            settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
            llm_enabled = str(settings_map.get("llm_enabled", "")).lower() in ("true", "1", "yes")
            llm_verify = str(settings_map.get("scan_llm_verify", "")).lower() in ("true", "1", "yes")
            use_llm = llm_enabled and llm_verify
            llm_cfg = {
                "base_url": settings_map.get("llm_base_url", ""),
                "api_key": settings_map.get("llm_api_key", ""),
                "model": settings_map.get("llm_model", ""),
                "system_prompt": settings_map.get("llm_system_prompt", ""),
            }
            # 合并当前模型的参数覆盖（llm_model_overrides：temperature/top_p/max_tokens）
            try:
                llm_cfg.update(llm_client.resolve_model_overrides(
                    settings_map, llm_cfg.get("model", "")))
            except Exception:
                pass
            with self._lock:
                self.state["llm_enabled"] = use_llm
            if use_llm:
                add_log(db, "info", "scan", "LLM 深度解析已启用（仅正文，不识别图片）")

            query = db.query(models.MonitorUser).filter(models.MonitorUser.status == "active")
            if user_ids:
                query = query.filter(models.MonitorUser.id.in_(user_ids))
            users = query.all()

            # ---- 断点续扫：跳过上次已完成的用户 ----
            resume_done = load_resume_done(db)
            if resume_done and not user_ids:
                before = len(users)
                users = [u for u in users if str(u.uid) not in resume_done]
                skipped = before - len(users)
                if skipped:
                    add_log(db, "info", "scan",
                            f"断点续扫：跳过上次已完成的 {skipped} 个用户")

            total = len(users)
            with self._lock:
                self.state["total"] = total
                self.state["message"] = (
                    f"从断点继续，剩余 {total} 个用户待扫描"
                    if resume_done and not user_ids
                    else f"共 {total} 个监控用户待扫描")

            all_candidates = []
            # ---- 流水线共享状态（扫描主线程 + LLM 解析 worker 跨线程共享） ----
            existing_ids = {r[0] for r in db.query(models.Activity.activity_id).all() if r[0]}
            inserted_ids = set()
            new_found = 0
            backfilled = 0
            need_user_seen = set()   # 本次扫描跨用户同动态去重
            # 时间回溯窗口（天），对齐 bilibinggo watch_backfill_days
            try:
                backfill_days = max(1, min(int(settings_map.get("watch_backfill_days", 10)), 90))
            except (TypeError, ValueError):
                backfill_days = 10
            # ---- 入库函数（跨线程安全：扫描主线程 + LLM 解析 worker 共用） ----
            # 去重：DB 已有（existing_ids）+ 本次新增（inserted_ids）都算；
            # 新增后同步并入 existing_ids，保证后续用户遇到同一动态不重复解析。
            def _ingest(sess, cands):
                """入库一批候选（notice/LLM 回填 + 新增），返回 (新增数, 回填数)"""
                added = 0
                bkf = 0
                for cand in cands:
                    if self._stop:
                        break
                    verdict = cand.get("_verdict")
                    if verdict is not None:
                        # LLM 有明确判定：以 LLM 结果为准（非抽奖则跳过）
                        is_lottery = verdict.get("is_lottery") is True
                    else:
                        # LLM 解析失败（异常）：回退关键词初筛/互动抽奖结果
                        is_lottery = cand.get("is_lottery") is True
                    if not is_lottery:
                        continue
                    with self._lock:
                        if cand["activity_id"] in inserted_ids:
                            continue
                        if cand["activity_id"] in existing_ids:
                            # 已存在：用 notice/LLM 结构化结果回填缺失字段
                            # （上一轮可能因 LLM 失败用关键词初筛入库，prize/end_time 缺失）
                            if verdict or cand.get("notice"):
                                exists = sess.query(models.Activity).filter_by(
                                    activity_id=cand["activity_id"]).first()
                                if exists and _backfill_activity(exists, verdict, cand):
                                    bkf += 1
                            continue
                    # 字段合并优先级：互动抽奖 notice（官方结构化）> LLM > 正文关键词提取
                    notice = cand.get("notice")
                    prize = ((bili_client.BiliClient.format_notice_prizes(notice)
                              if notice else None)
                             or (verdict or {}).get("prize")
                             or cand.get("prize_info", ""))
                    winner = (bili_client.BiliClient.notice_winner_count(notice)
                              if notice else 0) or (verdict or {}).get("winner_count") or 0
                    end_time = (bili_client.BiliClient.notice_end_time(notice)
                                if notice else None) \
                        or _parse_end_time(llm_client.fix_end_time_year(
                            str((verdict or {}).get("end_time") or ""),
                            desc or cand.get("desc", ""))) \
                        or _parse_end_time(llm_client.fix_end_time_year(
                            str(cand.get("end_time") or ""),
                            desc or cand.get("desc", "")))
                    title = (verdict or {}).get("title") or cand["title"]
                    desc = (verdict or {}).get("desc") or cand.get("desc", "")
                    # 已结束的活动直接标记 ended（不在待参与中显示、不可参与）：
                    # ①有 end_time 且已过期；②互动抽奖 notice 显示已开奖（有中奖名单/status 非进行中）
                    now = datetime.now()
                    if (end_time and end_time < now) or (
                            notice and bili_client.BiliClient.notice_is_ended(notice)):
                        init_status = "ended"
                    else:
                        init_status = "pending"
                    sess.add(models.Activity(
                        activity_id=cand["activity_id"],
                        title=str(title)[:200],
                        desc=desc,
                        link=cand["link"],
                        author_uid=cand["author_uid"],
                        author_name=cand["author_name"],
                        source_uid=cand["source_uid"],
                        source_name=cand["source_name"],
                        source_type=cand["source_type"],
                        prize_info=str(prize)[:200],
                        winner_count=int(winner) if isinstance(winner, int) else 0,
                        repost_count=cand.get("repost_count", 0) or 0,
                        publish_time=cand["publish_time"],
                        end_time=end_time,
                        comment_text=cand.get("comment_text", ""),
                        status=init_status,
                    ))
                    with self._lock:
                        inserted_ids.add(cand["activity_id"])
                        existing_ids.add(cand["activity_id"])
                    added += 1
                sess.commit()
                return added, bkf

            def _parse_and_ingest(batch, need_user, llm_cfg):
                """后台 worker：LLM 解析该用户候选 + 独立 session 入库（扫描同步进行）。

                批量模式固定使用内置 PARSE_BATCH_SYSTEM_PROMPT（数组输出契约），
                不传设置项自定义提示词（单条格式与批量数组格式不兼容）。
                """
                s = SessionLocal()
                try:
                    try:
                        res = llm_client.parse_lottery_activities_batch(
                            llm_cfg["base_url"], llm_cfg["api_key"], llm_cfg["model"],
                            batch, batch_size=len(batch),
                            temperature=llm_cfg.get("temperature"),
                            top_p=llm_cfg.get("top_p"),
                            max_tokens=llm_cfg.get("max_tokens"))
                    except Exception:
                        res = [None] * len(batch)
                    for j, verdict in enumerate(res):
                        if j >= len(need_user):
                            break
                        cand = need_user[j]
                        with self._lock:
                            self.state["llm_done"] += 1
                            self.state["llm_current"] = (cand.get("title") or "")[:20]
                            if verdict and verdict.get("is_lottery") is True:
                                self.state["llm_success"] += 1
                                cand["_verdict"] = verdict
                            else:
                                self.state["llm_fail"] += 1
                                # 保留原始 verdict：LLM 明确判定非抽奖时，
                                # 入库阶段不再回退关键词初筛（避免误入库）
                                cand["_verdict"] = verdict or None
                    batch_cands = [c for c in need_user if c.get("_verdict") is not None]
                    _added, _bkf = _ingest(s, batch_cands)
                    # 入库后立即启动评论池补齐线程（独立评论生成，与扫描并行不阻塞）
                    if (_added > 0
                            and llm_cfg.get("base_url") and llm_cfg.get("model")):
                        try:
                            threading.Thread(target=_gen_comment_pools,
                                             daemon=True).start()
                        except Exception:
                            pass
                    return _added, _bkf
                finally:
                    s.close()

            def _gen_comment_pools():
                """后台线程：补齐评论池（独立 session，与扫描并行不阻塞）"""
                s = SessionLocal()
                try:
                    from .participate_text_service import ensure_comment_pools
                    ensure_comment_pools(s, limit=8, newest_first=True)
                except Exception:
                    s.rollback()
                finally:
                    s.close()

            pending_futures = []   # LLM 解析后台任务（扫描与解析并行执行）
            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
                for idx, user in enumerate(users, 1):
                    if self._stop:
                        break
                    with self._lock:
                        self.state["done"] = idx - 1
                        self.state["current_user"] = f"{user.username}({user.uid})"
                        self.state["message"] = f"正在扫描 {user.username} 的动态..."
                    items = []
                    try:
                        items = client.get_space_dynamics(
                            user.uid, username=user.username,
                            source_type=user.monitor_type,
                            since_days=backfill_days,
                            stop_callback=lambda: self._stop)
                        for it in items:
                            it["_user"] = user
                        all_candidates.extend(items)
                        user.last_scanned_at = datetime.now()
                        db.commit()
                        add_log(db, "info", "scan",
                                f"扫描 {user.username}：抓取 {len(items)} 条候选动态")
                    except Exception as e:
                        db.rollback()
                        add_log(db, "error", "scan", f"扫描失败 {user.username}: {e}")
                    # 断点续扫：该用户处理完成（无论成败），实时记录到断点
                    if not user_ids:
                        resume_done.add(str(user.uid))
                        save_resume_done(db, resume_done)
                    with self._lock:
                        self.state["done"] = idx
                        self.state["found"] = new_found

                    # ---- 流水线：扫完一个用户立即处理该用户候选（不等全部扫完）----
                    user_cands = [c for c in items if c.get("is_lottery") is True]
                    need_user = []
                    for c in user_cands:
                        if c["activity_id"] in need_user_seen:
                            continue
                        with self._lock:
                            if c["activity_id"] in inserted_ids:
                                continue
                        if c["activity_id"] in existing_ids:
                            exists = db.query(models.Activity).filter_by(
                                activity_id=c["activity_id"]).first()
                            # 字段完整 -> 跳过 LLM；字段全空（解析失败入库）-> 仍提交回填
                            if exists and (exists.prize_info or exists.end_time
                                           or exists.winner_count):
                                continue
                        need_user.append(c)
                        need_user_seen.add(c["activity_id"])
                    if need_user:
                        if use_llm and llm_cfg.get("base_url") and llm_cfg.get("model"):
                            batch = [{"id": str(c["activity_id"]),
                                      "text": c.get("desc", "") or c.get("title", ""),
                                      "notice": c.get("notice")}
                                     for c in need_user[:LLM_BATCH]]
                            with self._lock:
                                self.state["llm_total"] += len(need_user)
                                self.state["message"] = (
                                    f"解析 {user.username} 的 {len(need_user)} 条动态...")
                            # 后台解析 + 入库（不阻塞继续扫下一个用户）
                            fut = pool.submit(_parse_and_ingest, batch, need_user, llm_cfg)
                            pending_futures.append(fut)
                        else:
                            # LLM 未启用：关键词初筛直接入库
                            _added, _bkf = _ingest(db, need_user)
                            new_found += _added
                            backfilled += _bkf
                            with self._lock:
                                self.state["found"] = new_found
                        # ---- 入库后立即启动评论池补齐线程（独立评论生成，扫描不等待）----
                        if (str(settings_map.get("participate_text_mode", "custom"))
                                in ("llm_generate", "random")
                                and llm_cfg.get("base_url") and llm_cfg.get("model")):
                            try:
                                threading.Thread(target=_gen_comment_pools,
                                                 daemon=True).start()
                            except Exception:
                                pass
                    # 实时回收已完成的解析任务（扫描中 found 实时更新）
                    if pending_futures and not self._stop:
                        done_set, _ = wait(pending_futures, timeout=0)
                        for fut in list(done_set):
                            try:
                                _a, _b = fut.result()
                                new_found += _a
                                backfilled += _b
                            except Exception:
                                pass
                            pending_futures.remove(fut)
                            with self._lock:
                                self.state["found"] = new_found
                    time.sleep(SCAN_SLEEP)

                # ---- 扫描完成，等待所有 LLM 解析任务结束 ----
                # 已停止：不再等待后台解析（daemon 线程自行结束），立即收尾
                if not self._stop:
                    for fut in as_completed(pending_futures):
                        try:
                            _added, _bkf = fut.result()
                            new_found += _added
                            backfilled += _bkf
                        except Exception:
                            pass
                        with self._lock:
                            self.state["found"] = new_found
                            if self.state["llm_total"]:
                                self.state["message"] = (
                                    f"LLM 解析 {self.state['llm_done']}/"
                                    f"{self.state['llm_total']}，已入库 {new_found} 个活动")

            # ---- 剩余候选兜底入库（LLM 未启用 / 解析失败回退关键词初筛） ----
            # 包含预筛跳过的已存在活动（notice 回填仍生效）
            remaining = [c for c in all_candidates if c.get("is_lottery") is True
                         and c["activity_id"] not in inserted_ids]
            _added, _bkf = _ingest(db, remaining)
            new_found += _added
            backfilled += _bkf
            with self._lock:
                self.state["found"] += new_found

            # ---- 回填各监控用户「发现活动数」（scanned_count）：按 Activity.source_uid 实时统计 ----
            try:
                from sqlalchemy import func as _func
                cnt_rows = (db.query(models.Activity.source_uid,
                                     _func.count(models.Activity.id))
                            .group_by(models.Activity.source_uid).all())
                cnt_map = {str(uid): n for uid, n in cnt_rows}
                for mu in db.query(models.MonitorUser).all():
                    mu.scanned_count = cnt_map.get(str(mu.uid), 0)
                db.commit()
            except Exception:
                db.rollback()

            # ---- 扫描结束：最终兜底补齐评论池（独立评论生成，不阻塞扫描） ----
            # 扫描时 LLM 只做活动解析，不再生成评论；
            # 评论由 ensure_comment_pools 单独生成（按 active 账号数，相关/简短/带 emoji）。
            comment_note = ""
            if (new_found > 0
                    and str(settings_map.get("participate_text_mode", "custom")) in ("llm_generate", "random")
                    and llm_cfg.get("base_url") and llm_cfg.get("model")):
                try:
                    threading.Thread(target=_gen_comment_pools, daemon=True).start()
                    comment_note = f"，后台生成评论池"
                except Exception:
                    pass

            llm_note = ""
            if use_llm and self.state["llm_total"]:
                llm_note = (f"，LLM 解析 {self.state['llm_done']}/{self.state['llm_total']}"
                            f"（抽奖 {self.state['llm_success']}，非抽奖 {self.state['llm_fail']}）")
            backfill_note = f"，回填 {backfilled} 条缺失字段" if backfilled else ""
            with self._lock:
                # 旧线程收尾：若已有更新的扫描线程接手（代际不同），不覆盖其状态
                if gen is not None and gen != self._generation:
                    pass
                else:
                    # 自然完成（非终止）：清空断点，下次从头扫
                    if not self._stop and not user_ids:
                        clear_resume_done(db)
                    self.state["running"] = False
                    self.state["message"] = (f"扫描完成，新增 {new_found} 个活动{llm_note}"
                                             f"{backfill_note}{comment_note}"
                                             if not self._stop else "扫描已停止")
                self.state["current_user"] = ""
                self.state["finished_at"] = datetime.now().strftime("%H:%M:%S")
            add_log(db, "info", "scan",
                    f"扫描任务结束，共新增 {new_found} 个抽奖活动{llm_note}"
                    f"{backfill_note}{comment_note}")
        except Exception as e:
            add_log(db, "error", "scan", f"扫描任务异常: {e}")
            with self._lock:
                if gen is not None and gen != self._generation:
                    pass
                else:
                    self.state["running"] = False
                    self.state["message"] = f"扫描异常: {e}"
        finally:
            db.close()


def _parse_end_time(text: str):
    """把 LLM 返回的 end_time 字符串转 datetime"""
    if not text:
        return None
    from datetime import datetime as _dt
    try:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                return _dt.strptime(str(text).strip(), fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _backfill_activity(activity, verdict: dict, cand: dict) -> bool:
    """用互动抽奖 notice / LLM 结构化结果回填已存在但字段缺失的活动记录。

    场景：某活动此前因 LLM 解析失败仅以关键词初筛入库，
    prize/end_time/winner_count 可能为空；notice/LLM 成功后可回填。
    字段优先级：互动抽奖 notice（官方结构化）> LLM > 已有值。
    注意：notice 是 B 站官方接口数据，命中时**直接覆盖**已有猜测值
    （LLM/关键词提取可能不准）；LLM 结果只填补空字段（已有值不覆盖）。
    返回是否发生了更新。
    """
    from . import bili_client
    from datetime import datetime as _dt_now
    changed = False
    notice = cand.get("notice")
    if notice:
        # ---- notice（官方结构化）：直接覆盖 ----
        prize = bili_client.BiliClient.format_notice_prizes(notice)
        if prize and prize != (activity.prize_info or "").strip():
            activity.prize_info = prize[:200]
            changed = True
        end_time = bili_client.BiliClient.notice_end_time(notice)
        if end_time and end_time != activity.end_time:
            activity.end_time = end_time
            changed = True
        winner = bili_client.BiliClient.notice_winner_count(notice)
        if winner > 0 and winner != activity.winner_count:
            activity.winner_count = winner
            changed = True
        if not (activity.desc or "").strip():
            activity.desc = cand.get("desc", "") or prize
            changed = True
        # notice 有开奖时间且已过期 -> 标记已结束（待参与中隐藏、不可参与）
        if (end_time and end_time < _dt_now.now()
                and activity.status in ("pending", "participated")):
            activity.status = "ended"
            changed = True
        return changed
    # ---- LLM 结果：只填补空字段 ----
    if not verdict:
        return changed
    prize = (verdict.get("prize") or "").strip()
    if prize and not (activity.prize_info or "").strip():
        activity.prize_info = prize[:200]
        changed = True
    end_time = _parse_end_time(verdict.get("end_time"))
    if end_time and activity.end_time is None:
        activity.end_time = end_time
        changed = True
    winner = verdict.get("winner_count")
    if isinstance(winner, int) and winner > 0 and not activity.winner_count:
        activity.winner_count = winner
        changed = True
    title = (verdict.get("title") or "").strip()
    if title and len(title) > len(activity.title or ""):
        # 关键词初筛的 title 常为正文截断（含 emoji/换行），LLM 标题更精炼
        activity.title = title[:200]
        changed = True
    desc = (verdict.get("desc") or "").strip()
    if desc and len(desc) > len(activity.desc or ""):
        activity.desc = desc
        changed = True
    # 回填出 end_time 且已过期 -> 标记已结束
    if (activity.end_time and activity.end_time < _dt_now.now()
            and activity.status in ("pending", "participated")):
        activity.status = "ended"
        changed = True
    return changed


scan_manager = ScanManager()


def scan_single_user(db, user) -> int:
    """同步扫描单个监控用户（活动发现页"扫描"按钮使用）"""
    from ..routers.logs import add_log
    client = _build_client(db)
    settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
    try:
        backfill_days = max(1, min(int(settings_map.get("watch_backfill_days", 10)), 90))
    except (TypeError, ValueError):
        backfill_days = 10
    items = client.get_space_dynamics(
        user.uid, username=user.username, source_type=user.monitor_type,
        since_days=backfill_days)
    found = 0
    for it in items:
        if it.get("is_lottery") is not True:
            continue
        exists = db.query(models.Activity).filter_by(
            activity_id=it["activity_id"]).first()
        if exists:
            continue
        db.add(models.Activity(
            activity_id=it["activity_id"],
            title=it["title"], desc=it["desc"], link=it["link"],
            author_uid=it["author_uid"], author_name=it["author_name"],
            source_uid=it["source_uid"], source_name=it["source_name"],
            source_type=it["source_type"],
            prize_info=it["prize_info"],
            winner_count=it.get("winner_count", 0) or 0,
            repost_count=it["repost_count"],
            publish_time=it["publish_time"],
            end_time=it.get("end_time"),
            # 已过期的活动直接标记 ended（待参与中不显示、不可参与）
            status=("ended" if it.get("end_time")
                    and it["end_time"] < datetime.now() else "pending"),
        ))
        found += 1
    user.last_scanned_at = datetime.now()
    # 发现活动数：按 Activity.source_uid 实时统计（覆盖，避免重复扫描累加虚高）
    try:
        from sqlalchemy import func as _func
        _cnt = (db.query(_func.count(models.Activity.id))
                .filter(models.Activity.source_uid == str(user.uid)).scalar()) or 0
        user.scanned_count = _cnt
    except Exception:
        pass
    # 质量剔除（通用，含职业号）：连续扫描无抽奖活动 → 计数累加，达到设置阈值
    # 标记失效（inactive）不再参与扫描——避免动态变化/误判的无效用户长期占用监控资源
    remove_after = int(settings_map.get("monitor_empty_scan_remove", PRO_EMPTY_LIMIT) or 0)
    if remove_after > 0 and user.status == "active":
        if found == 0:
            user.empty_scan_count = (user.empty_scan_count or 0) + 1
            if user.empty_scan_count >= remove_after:
                user.status = "inactive"
                tag = "职业号" if (user.note or "").find("职业") >= 0 else "监控用户"
                add_log(db, "warning", "monitor",
                        f"{tag} {user.username}（UID {user.uid}）连续 "
                        f"{user.empty_scan_count} 次扫描无抽奖活动，标记失效")
        else:
            user.empty_scan_count = 0
    db.commit()
    add_log(db, "success", "monitor",
            f"扫描 {user.username} 完成，发现 {found} 个抽奖活动")
    return found


def get_llm_settings(db) -> dict:
    """读取 LLM 相关设置（供扫描增强识别使用）"""
    rows = db.query(models.Setting).filter(
        models.Setting.key.like("llm_%")).all()
    return {r.key: r.value for r in rows}
