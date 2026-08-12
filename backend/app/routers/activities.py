"""活动列表：识别到的抽奖活动 增删改查 / 快速筛选 / 统计

对齐 bilibinggo 契约：
  GET /api/activities                活动表（类型/状态/热度/搜索筛选）
  GET /api/activities/triple-targets 三连目标
  POST /api/jobs {action: participate|participate_triple}
增强：活动详情、手动新增、编辑、删除、统计卡片、参与账号记录。
"""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, or_
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas
from .logs import add_log

router = APIRouter(prefix="/api/activities", tags=["activities"])


def _ser(a: models.Activity) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "link": a.link,
        "author_name": a.author_name,
        "source_name": a.source_name,
        "source_type": a.source_type,
        "prize_info": a.prize_info,
        "end_time": a.end_time.strftime("%Y-%m-%d %H:%M:%S") if a.end_time else "",
        "status": a.status,
        "reviewed_at": a.reviewed_at.strftime("%Y-%m-%d %H:%M:%S") if a.reviewed_at else "",
        "comment_text": a.comment_text or "",
        "participated_accounts": _parse_account_ids(a.participated_accounts),
    }


def _parse_account_ids(raw: str) -> list:
    import json as _json
    if not raw:
        return []
    try:
        val = _json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _default_account(db: Session):
    """默认使用第一个已登录账号"""
    return (db.query(models.Account)
            .filter_by(status="active")
            .order_by(models.Account.id.asc()).first())


@router.get("")
def list_activities(
    page: int = 1,
    page_size: int = 20,
    status: str = "",
    source_type: str = "",
    keyword: str = "",
    sort: str = "default",          # default / hot_desc / hot_asc / time_desc
    upcoming: bool = False,          # 即将开奖（已参与且近3天发布）
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    now = datetime.now()
    q = db.query(models.Activity)
    if status:
        q = q.filter(models.Activity.status == status)
        # 待参与 = 可参与 = 未过期；过期活动即使状态残留 pending 也强制过滤
        if status == "pending":
            q = q.filter(or_(models.Activity.end_time.is_(None),
                             models.Activity.end_time >= now))
    if source_type:
        q = q.filter(models.Activity.source_type == source_type)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(
            models.Activity.title.contains(like),
            models.Activity.desc.contains(like),
            models.Activity.author_name.contains(like),
            models.Activity.prize_info.contains(like),
        ))
    if upcoming:
        q = q.filter(models.Activity.status == "participated",
                     models.Activity.publish_time >= datetime.now() - timedelta(days=3))
    if start:
        try:
            q = q.filter(models.Activity.publish_time >= datetime.fromisoformat(start))
        except Exception:
            pass
    if end:
        try:
            q = q.filter(models.Activity.publish_time <=
                         datetime.fromisoformat(end) + timedelta(days=1))
        except Exception:
            pass

    if sort == "hot_desc":
        q = q.order_by(models.Activity.repost_count.desc())
    elif sort == "hot_asc":
        q = q.order_by(models.Activity.repost_count.asc())
    elif sort == "time_desc":
        q = q.order_by(models.Activity.publish_time.desc())
    else:
        # 默认排序：未过期（end_time >= now）优先、按结束时间升序（最近开奖的最前），
        # 已过期/无结束时间的排后面（先过期时间近的，无时间最后）
        now = datetime.now()
        q = q.order_by(
            case(
                (and_(models.Activity.end_time.isnot(None),
                      models.Activity.end_time >= now), 0),
                else_=1,
            ),
            models.Activity.end_time.is_(None),
            models.Activity.end_time.asc(),
        )

    total = q.count()
    items = (q.offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_ser(a) for a in items]}


@router.get("/stats")
def activity_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    from sqlalchemy import or_
    now = datetime.now()
    rows = (db.query(models.Activity.status, func.count(models.Activity.id))
            .group_by(models.Activity.status).all())
    counts = {k: v for k, v in rows}
    total = db.query(models.Activity).count()
    # pending 口径与全自动模式一致：未过期才计入（已过期但仍 pending 的按 ended 显示）
    pending = db.query(models.Activity).filter(
        models.Activity.status == "pending",
        or_(models.Activity.end_time.is_(None),
            models.Activity.end_time > now)).count()
    ended = counts.get("ended", 0) + (counts.get("pending", 0) - pending)
    # 待复核：两阶段解析第二阶段未跑的活动（后台复核持续消化）
    unreviewed = db.query(models.Activity).filter(
        models.Activity.status.in_(["pending", "participated"]),
        models.Activity.reviewed_at.is_(None)).count()
    return {
        "total": total,
        "pending": pending,
        "unreviewed": unreviewed,
        "participated": counts.get("participated", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0),
        "ended": ended,
    }


@router.get("/triple-targets")
def triple_targets(db: Session = Depends(get_db)):
    """三连参与目标：前 3 个该账号未参加活动（未过期优先、结束时间最近在前）

    多账号：活动被其他账号参与过（status=participated）不算已参与，
    按 participated_accounts 判断当前账号是否参与过。
    """
    account = _default_account(db)
    now = datetime.now()
    q = (db.query(models.Activity)
         .filter(models.Activity.status.in_(["pending", "participated"]),
                 or_(models.Activity.end_time.is_(None),
                     models.Activity.end_time >= now)))
    rows = (q.order_by(
                case(
                    (and_(models.Activity.end_time.isnot(None),
                          models.Activity.end_time >= now), 0),
                    else_=1,
                ),
                models.Activity.end_time.is_(None),
                models.Activity.end_time.asc(),
            )
            .limit(20).all())
    # 账号级过滤：跳过当前账号已参与过的
    if account:
        items = [a for a in rows
                 if account.id not in _parse_account_ids(a.participated_accounts)][:3]
    else:
        items = rows[:3]
    return {"items": [_ser(a) for a in items]}


@router.post("/batch-participate")
def batch_participate(body: schemas.BatchParticipateRequest,
                      db: Session = Depends(get_db)):
    """批量参与（用指定/默认账号），逐个启动异步参与任务（真实三连）

    每个活动通过 participation_service 后台执行点赞/关注/转发/评论，
    进度经 /api/activities/{id}/participate-progress 轮询。
    """
    from ..services.participation_service import start_participate
    account = None
    if body.account_id:
        account = db.get(models.Account, body.account_id)
        if not account:
            raise HTTPException(404, "账号不存在")
    else:
        account = _default_account(db)
    if not account:
        return {"ok": False, "message": "请先在账号管理登录至少一个账号",
                "count": 0, "results": []}

    results = []
    started_cnt = 0
    for aid in body.activity_ids[:200]:
        act = db.get(models.Activity, aid)
        if not act:
            results.append({"id": aid, "ok": False, "message": "活动不存在"})
            continue
        # 已结束/已过期活动不参与
        if act.status == "ended" or (act.end_time and act.end_time < datetime.now()):
            if act.status != "ended":
                act.status = "ended"
            results.append({"id": aid, "ok": False, "message": "活动已结束"})
            continue
        accounts = _parse_account_ids(act.participated_accounts)
        if account.id in accounts:
            results.append({"id": aid, "ok": True, "message": "该账号已参与"})
            continue
        # 异步启动真实参与（入队，串行执行）
        status = start_participate(activity_id=aid, account_id=account.id)
        if status in ("queued", "running"):
            started_cnt += 1
            results.append({"id": aid, "ok": True, "message": "已加入参与队列"})
        else:
            results.append({"id": aid, "ok": False, "message": "已在队列中"})
    db.commit()
    add_log(db, "success", "activity",
            f"批量参与启动：{account.username} 启动 {started_cnt} 个参与任务")
    return {"ok": True, "message": f"批量参与已启动 {started_cnt} 个（进度在活动列表查看）",
            "count": started_cnt, "results": results}


@router.post("/batch-delete")
def batch_delete_activities(body: schemas.BatchIdsRequest,
                            db: Session = Depends(get_db)):
    acts = db.query(models.Activity).filter(
        models.Activity.id.in_(body.ids)).all()
    for a in acts:
        db.delete(a)
    db.commit()
    add_log(db, "warning", "activity", f"批量删除活动 {len(acts)} 条")
    return {"ok": True, "count": len(acts)}


@router.get("/participate-status")
def participate_status():
    """全局参与状态：当前执行 + 队列（静态路径需在 /{activity_id} 前注册）"""
    from ..services.participation_service import get_queue_status
    return get_queue_status()


@router.get("/discover-pro/progress")
def discover_pro_progress():
    """职业号发现进度/结果轮询（静态路径需在 /{activity_id} 前注册）"""
    from ..services.pro_discovery import get_discovery_progress
    return get_discovery_progress()


@router.get("/{activity_id}")
def get_activity(activity_id: int, db: Session = Depends(get_db)):
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    return _ser(act)


@router.post("")
def create_activity(body: schemas.ActivityCreate, db: Session = Depends(get_db)):
    act = models.Activity(
        title=body.title, link=body.link, desc=body.desc,
        author_name=body.author_name, prize_info=body.prize_info,
        winner_count=body.winner_count, status=body.status, note=body.note,
        source_name="手动添加", source_type="manual",
        publish_time=datetime.now(),
        end_time=_parse_dt(body.end_time))
    db.add(act)
    db.commit()
    add_log(db, "info", "activity", f"手动新增活动 {act.title}")
    return _ser(act)


def _parse_dt(text: str):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except Exception:
        return None


@router.put("/{activity_id}")
def update_activity(activity_id: int, body: schemas.ActivityUpdate,
                    db: Session = Depends(get_db)):
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    data = body.model_dump(exclude_none=True)
    if "end_time" in data:
        data["end_time"] = _parse_dt(data["end_time"])
    for field, val in data.items():
        setattr(act, field, val)
    db.commit()
    return _ser(act)


@router.delete("/{activity_id}")
def delete_activity(activity_id: int, db: Session = Depends(get_db)):
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    db.delete(act)
    db.commit()
    add_log(db, "warning", "activity", f"删除活动 {act.title}")
    return {"ok": True}


@router.post("/{activity_id}/participate")
def participate(activity_id: int, body: schemas.ParticipateRequest,
                db: Session = Depends(get_db)):
    """参与活动（入队异步执行，支持进度/取消/排队）。

    立即返回 started/queued；实际执行在后台 worker 队列串行进行，
    进度经 GET /api/activities/{id}/participate-progress 轮询。
    """
    from ..services.participation_service import start_participate
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")

    # 已结束的活动不可参与
    if act.status == "ended":
        return {"ok": False, "message": "活动已结束，无法参与",
                "activity": _ser(act)}
    # 动态校验：end_time 已过期但状态未更新 -> 标记 ended 并拒绝
    if (act.end_time and act.end_time < datetime.now()
            and act.status not in ("won", "skipped")):
        act.status = "ended"
        db.commit()
        return {"ok": False, "message": "活动已结束，无法参与",
                "activity": _ser(act)}

    # 确定参与账号
    account = None
    if body.account_id:
        account = db.get(models.Account, body.account_id)
        if not account:
            raise HTTPException(404, "账号不存在")
    else:
        account = _default_account(db)
    if not account:
        return {"ok": False, "message": "请先在账号管理登录至少一个账号",
                "activity": _ser(act)}

    accounts = _parse_account_ids(act.participated_accounts)
    # 单行「参与」按钮用默认账号；若默认账号已参与但仍有其他账号未参与，
    # 自动切换到第一个未参与的 active 账号执行（用户期望：按未参与账号参与）
    if account.id in accounts:
        alt = (db.query(models.Account)
               .filter(models.Account.status == "active",
                       ~models.Account.id.in_(accounts or [0]))
               .order_by(models.Account.id.asc()).first())
        if alt:
            account = alt
        else:
            return {"ok": True, "message": f"账号 {account.username} 已参与过该活动",
                    "activity": _ser(act)}

    # 入队（若正在参与其他活动则排队等待）
    status = start_participate(activity_id=activity_id, account_id=account.id)
    if status == "duplicate":
        return {"ok": False, "message": "该活动该账号已在参与队列中",
                "started": False, "activity": _ser(act)}
    return {"ok": True, "started": True,
            "status": status,
            "message": (f"已加入参与队列（{account.username}）" if status == "queued"
                        else f"开始参与：{act.title}（{account.username}）"),
            "activity": _ser(act)}


@router.get("/{activity_id}/participate-progress")
def participate_progress(activity_id: int):
    """参与进度轮询（点赞/关注/转发/评论 逐步展示）"""
    from ..services.participation_service import get_progress
    return get_progress(activity_id)


@router.post("/{activity_id}/discover-pro")
def discover_pro(activity_id: int, db: Session = Depends(get_db)):
    """发现职业抽奖账号（异步）：分析评论区用户，70% 转发为抽奖则加入监控。

    立即返回；进度/结果经 GET /api/discover-pro/progress 轮询。
    """
    from ..services.pro_discovery import start_discovery
    act = db.get(models.Activity, activity_id)
    if not act:
        raise HTTPException(404, "活动不存在")
    ok, msg = start_discovery(activity_id)
    if ok:
        add_log(db, "info", "activity", f"启动职业抽奖号发现（活动 {act.title[:30]}）")
    return {"ok": ok, "message": msg, "started": ok}


@router.post("/{activity_id}/participate-cancel")
def participate_cancel(activity_id: int):
    """取消参与任务（排队中移除 / 执行中停止）"""
    from ..services.participation_service import cancel_participate
    ok = cancel_participate(activity_id=activity_id)
    return {"ok": ok, "message": "已取消参与" if ok else "未找到进行中的参与任务"}


@router.post("/participate-triple")
def participate_triple(db: Session = Depends(get_db)):
    """三连参与：串行参与前 3 个该账号未参加活动（真实点赞/转发/评论）

    多账号：活动被其他账号参与过（status=participated）仍可参与，
    按 participated_accounts 判断当前账号是否已参与。
    """
    from ..services import bili_actions, bili_client as bili_mod
    from ..services.participate_text_service import resolve_participate_text
    account = _default_account(db)
    if not account:
        return {"ok": False, "message": "请先在账号管理登录至少一个账号", "count": 0, "results": []}
    now = datetime.now()
    q = (db.query(models.Activity)
         .filter(models.Activity.status.in_(["pending", "participated"]),
                 or_(models.Activity.end_time.is_(None),
                     models.Activity.end_time >= now)))
    rows = (q.order_by(
                case(
                    (and_(models.Activity.end_time.isnot(None),
                          models.Activity.end_time >= now), 0),
                    else_=1,
                ),
                models.Activity.end_time.is_(None),
                models.Activity.end_time.asc(),
            )
            .limit(20).all())
    # 账号级过滤：跳过该账号已参与过的
    rows = [a for a in rows
            if account.id not in _parse_account_ids(a.participated_accounts)][:3]

    # 评论池补齐（参与前确保评论池足够账号数，避免逐个现场生成）
    try:
        from ..services.participate_text_service import ensure_comment_pools
        ensure_comment_pools(db, limit=15)
    except Exception:
        pass

    settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
    mode = settings_map.get("participate_text_mode", "custom")
    custom_text = settings_map.get("participate_text", "")
    llm_cfg = {
        "base_url": settings_map.get("llm_base_url", ""),
        "api_key": settings_map.get("llm_api_key", ""),
        "model": settings_map.get("llm_model", ""),
    } if mode in ("llm_generate", "random") else None
    act_client = None
    try:
        if account.cookies:
            act_client = bili_mod.BiliClient(bili_mod.cookies_from_json(account.cookies))
    except Exception:
        act_client = None

    results = []
    for act in rows:
        # 跳过正被参与队列处理的活动（防并发写竞态）
        from ..services.participation_service import is_activity_busy
        if is_activity_busy(act.id):
            results.append({"id": act.id, "title": act.title, "ok": False,
                            "message": "正在参与队列中，跳过"})
            continue
        accounts = _parse_account_ids(act.participated_accounts)
        if account.id in accounts:
            results.append({"id": act.id, "title": act.title, "ok": True,
                            "message": "已参与过"})
            continue
        # 解析参与文案：优先用预生成评论池（按账号取不同，秒用不等 LLM）；
        # 池为空才现场生成（LLM 失败才 fallback 兜底）
        from ..services.participate_text_service import pick_comment_for_account
        comment_text = pick_comment_for_account(act, account.id) or ""
        if not comment_text:
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
        # 真实互动
        action_errors = []
        if act_client is not None:
            try:
                detail = act_client.get_dynamic_detail(act.activity_id)
                rid, ctype = "", 17
                if detail:
                    rid, ctype = bili_actions.extract_comment_oid(detail)
                steps = ("like", "repost", "comment")
                if act.author_uid:
                    steps = ("like", "follow", "repost", "comment")
                exec_res = bili_actions.execute_participation(
                    act_client, dynamic_id=act.activity_id,
                    sender_uid=act.author_uid or "", comment_text=comment_text,
                    comment_rid=rid, comment_type=ctype, steps=steps)
                action_errors = exec_res.get("errors", [])
            except Exception as e:
                action_errors.append(f"互动异常: {e}")
        else:
            action_errors.append("账号无 cookies，仅本地记录")
        accounts.append(account.id)
        act.participated_accounts = json.dumps(accounts)
        # 状态语义：所有 active 账号都参与过才置 participated，否则保持 pending
        active_ids = [a.id for a in db.query(models.Account)
                      .filter_by(status="active").all()]
        if active_ids and all(aid in accounts for aid in active_ids):
            if act.status != "participated":
                act.status = "participated"
                act.participated_at = datetime.now()
        else:
            if act.status == "participated":
                act.status = "pending"
        ok = len(action_errors) == 0
        results.append({"id": act.id, "title": act.title, "ok": ok,
                        "message": "成功" if ok else "; ".join(action_errors[:2])})
        add_log(db, "success", "activity",
                f"三连参与 {act.title}：" + ("成功" if ok else "; ".join(action_errors[:2])))
    db.commit()
    success_cnt = sum(1 for r in results if r.get("ok"))
    add_log(db, "success", "activity",
            f"三连参与完成，成功 {success_cnt}/{len(results)} 个活动")
    return {"ok": True, "count": success_cnt, "total": len(results),
            "results": results}


@router.post("/refresh-status")
def refresh_status(db: Session = Depends(get_db)):
    """刷新任务状态：过期活动标记已结束（中奖结果由用户自行判断，系统不自动判定）"""
    rows = db.query(models.Activity).filter(
        models.Activity.status.in_(["participated", "pending"])).all()
    changed = 0
    now = datetime.now()
    for act in rows:
        # 已到开奖时间（end_time 已过）-> 已结束
        if act.end_time and act.end_time < now:
            act.status = "ended"
            changed += 1
    db.commit()
    add_log(db, "info", "activity", f"刷新任务状态完成，{changed} 个活动状态更新")
    return {"ok": True, "changed": changed}
