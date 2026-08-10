"""参与任务异步执行器（队列 + 进度展示 + 可取消）

设计（对齐用户需求）：
1. 评论复用：优先用扫描时预生成的 act.comment_text（at_parse 批量预生成），
   没有预生成才现场生成——避免单个参与还要 LLM 生成一次（太慢）。
2. 全局队列：所有参与请求进队列，单 worker 串行执行（天然防风控）。
   重复点击参与 -> 排队（前端显示队列位置），不冲突。
3. 可取消：任务执行中可 cancel（动作步间检查，中断后续动作）。

状态：
  _participating[activity_id] = {running, queued, phase, step_index, total,
                                 action, message, results, errors, done,
                                 cancel_requested, queue_pos}
  phase: queued(排队中) / resolving(解析文案) / acting(执行互动) / done
"""
import copy
import threading
import time
from datetime import datetime

from ..database import SessionLocal
from .. import models

_participating: dict = {}
_queue: list = []          # 待执行任务 [(activity_id, account_id), ...]
_current: tuple | None = None   # 当前执行中的 (activity_id, account_id)
_lock = threading.Lock()
RETENTION_SEC = 300   # 完成状态保留 5 分钟供前端查询，之后清理


def _cleanup():
    """清理超过保留期的已完成状态"""
    now = time.time()
    stale = [aid for aid, st in _participating.items()
             if not st.get("running") and not st.get("queued") and st.get("done")
             and now - st.get("_ts", 0) > RETENTION_SEC]
    for aid in stale:
        _participating.pop(aid, None)


def get_progress(activity_id: int) -> dict:
    with _lock:
        _cleanup()
        st = _participating.get(activity_id)
        if st is None:
            return {
                "running": False, "queued": False, "phase": "idle", "step_index": 0,
                "total": 0, "action": "", "message": "", "results": [], "errors": [],
                "done": False, "queue_pos": 0,
            }
        return copy.deepcopy(st)


def get_queue_status() -> dict:
    """全局参与状态：当前执行任务 + 队列长度 + 各排队任务"""
    with _lock:
        _cleanup()
        running = None
        if _current:
            running = {
                "activity_id": _current[0],
                "account_id": _current[1],
                "message": (_participating.get(_current[0]) or {}).get("message", ""),
            }
        queued = []
        for i, (aid, acc_id) in enumerate(_queue, 1):
            queued.append({"activity_id": aid, "account_id": acc_id, "queue_pos": i})
        return {"running": running, "queued": queued, "queue_len": len(_queue)}


def is_activity_busy(activity_id: int) -> bool:
    """活动是否正在参与队列中（执行中或排队中）。

    供 auto_service / participate-triple 等同步路径使用：
    若活动正被队列处理，跳过避免并发写 participated_accounts 竞态。
    """
    with _lock:
        if _current and _current[0] == activity_id:
            return True
        return any(aid == activity_id for aid, _ in _queue)


def _update(activity_id: int, **kw):
    with _lock:
        state = _participating.setdefault(activity_id, {})
        state.update(kw)
        state["_ts"] = time.time()


def start_participate(*, activity_id: int, account_id: int) -> str:
    """加入参与队列。

    返回: 'queued'（已排队）/ 'duplicate'（同活动同账号已在队列或执行）
    """
    with _lock:
        _cleanup()
        # 同 activity+account 已在队列或执行中 -> 重复
        if (_current and _current[0] == activity_id and _current[1] == account_id):
            return "duplicate"
        for q_aid, q_acc in _queue:
            if q_aid == activity_id and q_acc == account_id:
                return "duplicate"
        # 排队
        queue_pos = len(_queue) + 1
        _queue.append((activity_id, account_id))
        _participating[activity_id] = {
            "running": True, "queued": True, "phase": "queued",
            "step_index": 0, "total": 0, "action": "",
            "message": f"排队中（第 {queue_pos} 位）...",
            "results": [], "errors": [], "done": False,
            "cancel_requested": False, "queue_pos": queue_pos, "_ts": time.time(),
        }
    _ensure_worker()
    return "queued"


def cancel_participate(*, activity_id: int) -> bool:
    """取消参与任务：排队中直接移除；执行中设取消标志（步间中断）"""
    with _lock:
        st = _participating.get(activity_id)
        if not st:
            return False
        # 排队中：直接移除
        removed = False
        for i, (q_aid, q_acc) in enumerate(_queue):
            if q_aid == activity_id:
                _queue.pop(i)
                removed = True
                break
        if removed:
            _participating.pop(activity_id, None)
        # 执行中：设取消标志
        elif st.get("running") and not st.get("done"):
            st["cancel_requested"] = True
            st["message"] = "正在停止..."
            return True
        else:
            return False
    # 锁外更新后续排队位置
    if removed:
        with _lock:
            for j, (q_aid, q_acc) in enumerate(_queue, 1):
                qst = _participating.setdefault(q_aid, {})
                qst.update(queue_pos=j, message=f"排队中（第 {j} 位）...", _ts=time.time())
    return True


_worker_thread = None
_worker_lock = threading.Lock()


def _ensure_worker():
    """确保 worker 线程在运行（单例）"""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
            _worker_thread.start()


def _worker_loop():
    """队列 worker：串行取任务执行"""
    global _current
    while True:
        with _lock:
            if not _queue:
                _current = None
                return
            task = _queue.pop(0)
            _current = task
        # 锁外更新排队位置（避免锁内调 _update 死锁：Lock 非重入）
        with _lock:
            for i, (q_aid, q_acc) in enumerate(_queue, 1):
                st = _participating.setdefault(q_aid, {})
                st.update(queue_pos=i, message=f"排队中（第 {i} 位）...", _ts=time.time())
        activity_id, account_id = task
        try:
            _run(activity_id, account_id)
        except Exception:
            pass
        with _lock:
            _current = None


def _run(activity_id: int, account_id: int):
    from ..routers.logs import add_log
    from ..services import bili_actions, bili_client as bili_mod
    from ..services.participate_text_service import resolve_participate_text
    db = SessionLocal()
    try:
        act = db.get(models.Activity, activity_id)
        if not act:
            _update(activity_id, running=False, queued=False, phase="done", done=True,
                    errors=["活动不存在"])
            return
        account = db.get(models.Account, account_id)
        if not account:
            _update(activity_id, running=False, queued=False, phase="done", done=True,
                    errors=["账号不存在"])
            return
        # 执行前复查：队列等待期间该账号可能已被其他入口参与（全自动/三连），避免重复
        import json as _json
        _existing = []
        try:
            _existing = _json.loads(act.participated_accounts or "[]")
            if not isinstance(_existing, list):
                _existing = []
        except Exception:
            _existing = []
        if account.id in _existing:
            _update(activity_id, running=False, queued=False, phase="done", done=True,
                    message="已参与过", result_text=f"账号 {account.username} 已参与过该活动",
                    results=[], errors=[])
            return

        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
        mode = settings_map.get("participate_text_mode", "custom")
        custom_text = settings_map.get("participate_text", "")
        gen_time = settings_map.get("participate_text_gen_time", "at_parse")
        llm_cfg = {
            "base_url": settings_map.get("llm_base_url", ""),
            "api_key": settings_map.get("llm_api_key", ""),
            "model": settings_map.get("llm_model", ""),
        } if mode in ("llm_generate", "random") else None
        act_client = None
        try:
            if account.cookies:
                act_client = bili_mod.BiliClient(
                    bili_mod.cookies_from_json(account.cookies))
        except Exception:
            act_client = None

        # ---- 解析参与文案 ----
        # 优先复用预生成评论池（按账号取不同评论，秒用不等待 LLM）；
        # 没有预生成才现场生成（LLM/随机/自定义）。
        from .participate_text_service import pick_comment_for_account
        comment_text = pick_comment_for_account(act, account.id) or ""
        source = "pre_generated" if comment_text else "custom"
        pool_size = 0
        if not comment_text:
            _update(activity_id, queued=False, phase="resolving",
                    message="生成参与文案...")
            res = resolve_participate_text(
                mode=mode, custom_text=custom_text,
                fallback_text="关注+转发，支持一下，谢谢！",
                client=act_client, dynamic_id=act.activity_id,
                activity_text=(act.desc or "") or act.title or "",
                llm_cfg=llm_cfg,
                allow_network=mode in ("random_comment", "llm_generate", "random"))
            comment_text = res["text"]
            source = res["source"]
            pool_size = res.get("pool_size", 0)
            if not act.comment_text and mode in ("random_comment", "llm_generate"):
                act.comment_text = comment_text

        # 取消检查：文案解析后
        if _is_cancelled(activity_id):
            _finish_cancelled(activity_id, db)
            return

        # ---- 标记已参与（入库） ----
        import json as _json
        accounts = []
        try:
            accounts = _json.loads(act.participated_accounts or "[]")
            if not isinstance(accounts, list):
                accounts = []
        except Exception:
            accounts = []
        if account.id not in accounts:
            accounts.append(account.id)
            act.participated_accounts = _json.dumps(accounts)
        # 状态语义：还有账号未参与 -> 保持 pending（留在待参与列表，可继续用其他账号参与）；
        # 所有 active 账号都已参与 -> 才置 participated。
        active_ids = [a.id for a in db.query(models.Account)
                      .filter_by(status="active").all()]
        if active_ids and all(aid in accounts for aid in active_ids):
            if act.status != "participated":
                act.status = "participated"
                act.participated_at = datetime.now()
        else:
            # 仍有账号可参与：留在待参与（含历史已置 participated 的活动恢复 pending）
            if act.status == "participated":
                act.status = "pending"
        db.commit()

        # ---- 真实互动：点赞/关注/转发/评论（逐步回调进度） ----
        action_results = []
        action_errors = []
        steps = ("like", "repost", "comment")
        if act.author_uid:
            steps = ("like", "follow", "repost", "comment")

        def on_step(step_index, total, action, detail):
            _update(activity_id, queued=False, phase="acting",
                    step_index=step_index, total=total, action=action, message=detail)

        if act_client is not None:
            try:
                detail = act_client.get_dynamic_detail(act.activity_id)
                rid, ctype = "", 17
                if detail:
                    rid, ctype = bili_actions.extract_comment_oid(detail)
                exec_res = bili_actions.execute_participation(
                    act_client, dynamic_id=act.activity_id,
                    sender_uid=act.author_uid or "", comment_text=comment_text,
                    comment_rid=rid, comment_type=ctype, steps=steps,
                    on_step=on_step)
                action_results = exec_res.get("results", [])
                action_errors = exec_res.get("errors", [])
            except Exception as e:
                action_errors.append(f"执行互动异常: {e}")
        else:
            action_errors.append("账号无 cookies，已跳过真实互动（仅本地记录）")

        # ---- 汇总 ----
        source_note = {"custom": "自定义", "random_comment": f"借用评论(池{pool_size})",
                       "llm_generate": "LLM生成", "pre_generated": "预生成",
                       "custom_fallback": "自定义兜底"}.get(source, source)
        action_note = ""
        if action_results:
            ok_cnt = sum(1 for x in action_results if x.get("ok"))
            action_note = f"，互动 {ok_cnt}/{len(action_results)} 项成功"
        if action_errors:
            action_note += f"，失败: {'; '.join(action_errors[:3])}"
        add_log(db, "success", "activity",
                f"账号 {account.username} 已参与活动：{act.title}"
                f"（文案[{source_note}] {comment_text[:30]}）{action_note}")
        _update(activity_id, running=False, queued=False, phase="done", done=True,
                message="参与完成", results=action_results, errors=action_errors,
                result_text=f"账号 {account.username} 参与完成{action_note}",
                comment_text=comment_text, comment_source=source)
        # 参与完成后：补齐评论池（不足账号数的活动重新生成，参与秒用不等 LLM）
        try:
            from .participate_text_service import ensure_comment_pools
            ensure_comment_pools(db, limit=15)
        except Exception:
            pass
    except Exception as e:
        add_log(db, "error", "activity", f"参与活动异常（id={activity_id}）: {e}")
        _update(activity_id, running=False, queued=False, phase="done", done=True,
                errors=[f"参与异常: {e}"], message="参与失败")
    finally:
        db.close()


def _is_cancelled(activity_id: int) -> bool:
    with _lock:
        st = _participating.get(activity_id) or {}
        return bool(st.get("cancel_requested"))


def _finish_cancelled(activity_id: int, db):
    """取消完成：清理状态"""
    from ..routers.logs import add_log
    _update(activity_id, running=False, queued=False, phase="done", done=True,
            message="已取消", results=[], errors=["已手动取消"])
    add_log(db, "warning", "activity", f"参与已取消（id={activity_id}）")
