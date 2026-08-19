"""参与文案解析：自定义文案池 / LLM 生成贴合评论（随机混合）

对齐 bilibinggo participate-text 语义：
- custom:        从设置项 participate_text（多行，一行一条评论）中随机挑一条
- llm_generate:  用 LLM 根据活动正文生成贴合该活动的转发评论
- random:        随机混合：随机从【LLM 贴合评论 / 自定义文案池】中挑一种，
                 失败自动降级到自定义文案池

调用方：activities.py participate / participate-triple / batch-participate
"""
import random
import threading
import time

from . import bili_client
from . import llm_client

MAX_TEXT_LEN = 100

# B 站评论自带表情代码（文本代码，B 站会渲染成表情图；比 emoji 更贴地气）
# 只收录评论语境通用、确定有效的代码；供 LLM 提示词参考与文案池使用。
BILI_EMOJI_CODES = [
    "[doge]", "[妙啊]", "[捂脸]", "[笑哭]", "[星星眼]", "[打call]",
    "[鼓掌]", "[害羞]", "[惊喜]", "[喜欢]", "[思考]", "[惊讶]",
    "[委屈]", "[亲亲]", "[吃瓜]", "[酸了]", "[灵光一现]", "[奋斗]",
    "[OK]", "[歪嘴笑]", "[抱拳]", "[再见]", "[无语]", "[哦豁]",
]

# 自定义文案池解析：participate_text 设置项为多行文本（一行一条评论），
# 供 custom / random 模式及兜底使用——用户完全掌控文案内容，热更新。


def pick_custom_pool_comment(custom_text: str) -> str:
    """从自定义文案池（多行文本，一行一条）随机挑一条；空返回空串"""
    for line in (custom_text or "").splitlines():
        if line.strip():
            return random.choice([l.strip() for l in (custom_text or "").splitlines() if l.strip()])
    return ""


# LLM 生成评论的提示词（理解驱动：简短随意，去 AI 味）
# 注意：sensenova 是 reasoning 模型，复杂提示词会先长思考再输出，
# 思考过长会占满 max_tokens 导致 content 截断——要求"直接输出，不要思考过程"。
LLM_COMMENT_PROMPT = (
    "你是哔哩哔哩普通用户，正在刷到一条抽奖/福利动态，随手留条评论。"
    "直接输出一条 3~25 字的评论，不要输出任何思考过程、解释或前缀。"
    "**长短要随机**：偶尔像真人随手打的极短评（2~6 字，如『好运』『抽我』『蹲』『冲』『羡慕了』），"
    "偶尔正常一句话（8~25 字），不要总是同一种长度。要求：\n"
    "①要贴合这条动态——提到奖品、作品/游戏名、角色、话题等具体信息，"
    "让人觉得你真看过内容，不是复制粘贴；\n"
    "②像真人随手打的：口语化、随意，可以有语气词、网络梗，甚至带点调侃；\n"
    "③表情自然：最多用 1 个 B 站表情代码（如 [doge] [妙啊] [星星眼] [打call] [鼓掌]，"
    "B 站会渲染成表情图，比 emoji 更贴地气），能不用就不用；"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型 emoji，真人不会那么发；\n"
    "④千万不要说「已转发关注三连」「希望能中奖」「坐等开奖」「求好运」"
    "「祝自己」这类机器人口号，也不要说「我真的很想要」这种客套话；\n"
    "⑤只输出评论本身，不要引号。"
)


def generate_llm_comment(llm_cfg: dict, activity_text: str) -> str | None:
    """用 LLM 生成贴合活动正文的转发评论

    sensenova 是 reasoning 模型：即使 chat_template_kwargs 关思考，
    仍会产生 4k~13k 字符 reasoning。max_tokens 必须给足（16384），
    否则 reasoning 占满预算导致 content 缺失（finish_reason=length）。
    实测：max_tokens=4096 时 ~30% 概率 content 为空；16384 稳定 4/4。
    """
    if not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        return None
    try:
        reply = llm_client.chat(
            llm_cfg["base_url"], llm_cfg.get("api_key", ""), llm_cfg["model"],
            [
                {"role": "system", "content": LLM_COMMENT_PROMPT},
                {"role": "user", "content": f"活动正文：\n{(activity_text or '')[:800]}"},
            ],
            temperature=float(llm_cfg.get("temperature", 0.9)),
            top_p=float(llm_cfg.get("top_p", 1.0)), max_tokens=llm_cfg.get("max_tokens"),
            extra_body={"chat_template_kwargs": {"thinking": False}})
        text = (reply or "").strip().strip('"\'「」')
        return text[:MAX_TEXT_LEN] if text else None
    except Exception:
        # 部分服务商不支持 thinking 参数（4xx）-> 回退不带该参数
        try:
            reply = llm_client.chat(
                llm_cfg["base_url"], llm_cfg.get("api_key", ""), llm_cfg["model"],
                [
                    {"role": "system", "content": LLM_COMMENT_PROMPT},
                    {"role": "user", "content": f"活动正文：\n{(activity_text or '')[:800]}"},
                ],
                temperature=float(llm_cfg.get("temperature", 0.9)),
                top_p=float(llm_cfg.get("top_p", 1.0)), max_tokens=llm_cfg.get("max_tokens"))
            text = (reply or "").strip().strip('"\'「」')
            return text[:MAX_TEXT_LEN] if text else None
        except Exception:
            return None


BATCH_COMMENT_PROMPT = (
    "你是哔哩哔哩普通用户，正在刷到多条抽奖/福利动态，随手留评论。"
    "直接输出 JSON，不要输出任何思考过程、解释或前缀。对每条动态写一条 3~25 字的评论。"
    "**长短要随机**：偶尔是极短评（2~6 字，如『好运』『抽我』『蹲』『冲』『羡慕了』），"
    "偶尔是正常一句话（8~25 字），不要总同一种长度。要求：\n"
    "①贴合该条动态——提到奖品、作品/游戏名、角色、话题等具体信息，"
    "让人觉得你真看过内容；\n"
    "②像真人随手打的：口语化、随意，可以有语气词、网络梗，甚至带点调侃；\n"
    "③表情自然：最多用 1 个 B 站表情代码（如 [doge] [妙啊] [星星眼] [打call] [鼓掌]，"
    "B 站会渲染成表情图，比 emoji 更贴地气），能不用就不用；"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型 emoji；\n"
    "④千万不要说「已转发关注三连」「希望能中奖」「坐等开奖」「求好运」"
    "这类机器人口号，不要客套话；\n"
    "⑤每条只输出评论本身，不要引号。\n"
    "只输出一个 JSON 数组，不要输出其他文字：\n"
    '[{"id": "动态id", "comment": "评论内容"}, ...]'
)


def generate_llm_comments_batch(llm_cfg: dict, items: list, batch_size: int = 10) -> dict:
    """批量生成多条评论（解析时预生成用，一次请求）。

    items: [{"id": str, "text": str}, ...]
    返回: {id: comment_text}，失败条目不包含
    """
    if not items or not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        return {}
    result = {}
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        try:
            lines = [f"【动态 {j}】id={it['id']}\n{(it.get('text') or '')[:800]}"
                     for j, it in enumerate(batch, 1)]
            reply = llm_client.chat(
                llm_cfg["base_url"], llm_cfg.get("api_key", ""), llm_cfg["model"],
                [
                    {"role": "system", "content": BATCH_COMMENT_PROMPT},
                    {"role": "user", "content": "\n\n".join(lines)},
                ],
                temperature=float(llm_cfg.get("temperature", 0.9)),
            top_p=float(llm_cfg.get("top_p", 1.0)), max_tokens=llm_cfg.get("max_tokens"),
                extra_body={"chat_template_kwargs": {"thinking": False}})
            arr = llm_client._extract_json_array(reply or "")
            if arr:
                for x in arr:
                    if isinstance(x, dict) and x.get("id") and x.get("comment"):
                        c = str(x["comment"]).strip().strip('"\'「」')
                        if c:
                            result[str(x["id"])] = c[:MAX_TEXT_LEN]
        except Exception:
            continue
    return result


# 评论池批量生成提示词：一次请求为多个活动各生成 count 条不同评论
POOL_COMMENTS_PROMPT = (
    "你是一名 B 站资深用户，正在参加 UP 主发起的抽奖活动。"
    "请为下面每个【动态】分别生成 {count} 条参与抽奖的评论。\n"
    "要求：\n"
    "1. 每条评论都要贴合对应动态的内容（奖品、主题、UP主），口语化、真诚\n"
    "2. 简短（不超过 40 字）\n"
    "3. 表情自然：最多用 1 个 B 站表情代码（如 [doge] [妙啊] [星星眼] [打call] [鼓掌]，"
    "B 站会渲染成表情图，比 emoji 更贴地气），能不用就不用；"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型 emoji\n"
    "4. 同一动态的 {count} 条评论必须各不相同（语气/角度有差异，像不同账号发的）\n"
    "5. 不要提\"抽奖\"\"中奖\"\"转发抽\"等字眼，像真实粉丝留言\n"
    "只输出 JSON 数组，格式：\n"
    '[{{"id": "动态id", "comments": ["评论1", "评论2", ...]}}, ...]'
)


def generate_comment_pools_batch(llm_cfg: dict, items: list, count: int,
                                 batch_size: int = 5, retries: int = 2) -> dict:
    """批量生成评论池：一次 LLM 请求多个活动，每活动生成 count 条不同评论。

    items: [{"id": str, "text": str}, ...]
    返回: {id: [评论1, 评论2, ...]}；失败重试后仍失败的活动不包含。
    """
    if not items or count <= 0 or not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        return {}
    result = {}
    prompt = POOL_COMMENTS_PROMPT.format(count=count)
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        lines = [f"【动态 {j}】id={it['id']}\n{(it.get('text') or '')[:800]}"
                 for j, it in enumerate(batch, 1)]
        for attempt in range(retries + 1):
            try:
                reply = llm_client.chat(
                    llm_cfg["base_url"], llm_cfg.get("api_key", ""), llm_cfg["model"],
                    [{"role": "system", "content": prompt},
                     {"role": "user", "content": "\n\n".join(lines)}],
                    temperature=float(llm_cfg.get("temperature", 0.9)),
            top_p=float(llm_cfg.get("top_p", 1.0)), max_tokens=llm_cfg.get("max_tokens"),
                    extra_body={"chat_template_kwargs": {"thinking": False}})
                arr = llm_client._extract_json_array(reply or "")
                if arr:
                    for x in arr:
                        if not (isinstance(x, dict) and x.get("id")):
                            continue
                        cmts = x.get("comments") or []
                        if isinstance(cmts, list):
                            cleaned = [str(c).strip().strip('"\'「」')[:MAX_TEXT_LEN]
                                       for c in cmts if str(c).strip()]
                            if cleaned:
                                result[str(x["id"])] = cleaned[:count]
                    break   # 拿到结果即退出重试（个别缺失不重试整批）
            except Exception:
                if attempt >= retries:
                    continue
    return result


COMMENT_BUFFER_THRESHOLD = 3    # 待参与活动中无评论的数量 ≤ 3 时触发预生成
COMMENT_BUFFER_BATCH = 10       # 每次预生成条数（按开奖时间最近的取）


def _parse_comment_pool(raw: str) -> list:
    """解析 comment_text：兼容 JSON 数组（多条评论池）与单条文本。

    comment_text 可能存：["评论1","评论2",...]（多账号各用一条）或 "单条评论"。
    返回 list；空返回 []。
    """
    if not raw:
        return []
    import json as _json
    try:
        val = _json.loads(raw)
        if isinstance(val, list) and val:
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    text = str(raw).strip()
    return [text] if text else []


def pick_comment_for_account(act, account_id: int) -> str | None:
    """按账号从活动评论池中取一条（多账号各有不同评论；单条则共用）。

    act: 数据库 Activity 对象（需有 comment_text / participated_accounts）
    返回评论文本；无则 None。
    """
    import json as _json
    pool = _parse_comment_pool(act.comment_text or "")
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    # 多账号：按该账号在 participated_accounts 中的序号取不同评论（稳定映射）
    accounts = []
    try:
        accounts = _json.loads(act.participated_accounts or "[]")
        if not isinstance(accounts, list):
            accounts = []
    except Exception:
        accounts = []
    idx = accounts.index(account_id) if account_id in accounts else 0
    return pool[idx % len(pool)]


def generate_comments_for_activity(llm_cfg: dict, activity_text: str,
                                   count: int) -> list:
    """为单个活动生成 count 条**不同**的贴合评论（一次请求，多账号各用一条）。

    返回 list[str]；失败返回 []。
    """
    if count <= 0 or not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        return []
    prompt = (
        "你是哔哩哔哩普通用户，正在刷到一条抽奖/福利动态，随手留评论。"
        f"直接输出 JSON，不要输出任何思考过程、解释或前缀。"
        f"为这条动态写 {count} 条**内容各不相同**的评论，每条 3~25 字。"
        f"**长短要随机**：偶尔极短评（2~6 字，如『好运』『抽我』『蹲』『冲』『羡慕了』），"
        f"偶尔正常一句话（8~25 字），不要总同一种长度。要求：\n"
        "①贴合这条动态——提到奖品、作品/游戏名、角色、话题等具体信息，"
        "让人觉得你真看过内容；\n"
        "②像真人随手打的：口语化、随意，可以有语气词、网络梗，甚至带点调侃；\n"
        "③不要堆表情：最多用一个常见表情（😂😆👍🥰🔥 等），能不用就不用，"
        "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型表情；\n"
        "④千万不要说「已转发关注三连」「希望能中奖」「坐等开奖」「求好运」"
        "这类机器人口号，不要客套话；\n"
        "⑤只输出评论本身，不要引号。\n"
        '只输出一个 JSON 字符串数组，不要输出其他文字：["评论1", "评论2", ...]'
    )
    try:
        reply = llm_client.chat(
            llm_cfg["base_url"], llm_cfg.get("api_key", ""), llm_cfg["model"],
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"活动正文：\n{(activity_text or '')[:800]}"},
            ],
            temperature=float(llm_cfg.get("temperature", 0.9)),
            top_p=float(llm_cfg.get("top_p", 1.0)), max_tokens=llm_cfg.get("max_tokens"),
            extra_body={"chat_template_kwargs": {"thinking": False}})
        arr = llm_client._extract_json_array(reply or "")
        if not arr and reply:
            # 兼容返回纯数组文本的情况
            import json as _json
            try:
                arr = _json.loads((reply or "").strip())
            except Exception:
                arr = None
        if not arr:
            return []
        comments = [str(x).strip().strip('"\'「」') for x in arr
                    if isinstance(x, str) and str(x).strip()]
        return [c[:MAX_TEXT_LEN] for c in comments][:count]
    except Exception:
        return []


def _mode_uses_llm(mode: str) -> bool:
    """该模式是否使用 LLM 生成评论（llm_generate / random 混合模式）"""
    return mode in ("llm_generate", "random")


def ensure_next_comments(db) -> int:
    """参与完成后调用：为「队列中下一个要参与的活动」提前批量生成评论。

    数量 = 该活动还需参与的账号数（每个账号一条不同评论，参与时秒用不等 LLM）。
    返回生成条数；无队列/无需生成/LLM 未配置返回 0。
    """
    from datetime import datetime
    from .. import models
    try:
        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
        if not _mode_uses_llm(settings_map.get("participate_text_mode", "custom")):
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
        # 找队列中下一个待执行活动
        from .participation_service import get_queue_status
        st = get_queue_status()
        next_id = None
        if st.get("running") and st.get("running").get("activity_id"):
            next_id = st["running"]["activity_id"]
        elif st.get("queued"):
            next_id = st["queued"][0]["activity_id"]
        if not next_id:
            return 0
        act = db.get(models.Activity, next_id)
        if not act or _parse_comment_pool(act.comment_text or ""):
            return 0
        # 计算还需参与账号数（未参与该活动的 active 账号）
        import json as _json
        accs = []
        try:
            accs = _json.loads(act.participated_accounts or "[]")
            if not isinstance(accs, list):
                accs = []
        except Exception:
            accs = []
        active_ids = [a.id for a in db.query(models.Account)
                      .filter_by(status="active").all()]
        need = [aid for aid in active_ids if aid not in accs]
        if not need:
            return 0
        text = (act.desc or "") or (act.title or "")
        comments = generate_comments_for_activity(llm_cfg, text, len(need))
        if comments:
            act.comment_text = _json.dumps(comments, ensure_ascii=False)
            db.commit()
        return len(comments)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def ensure_comment_buffer(db) -> int:
    """评论预生成 buffer：保证待参与活动随时有预生成评论，参与时秒用不等 LLM。

    触发条件：待参与（pending/participated 且未过期）活动中 comment_text 为空的
    数量 <= COMMENT_BUFFER_THRESHOLD(3) 时，批量预生成 COMMENT_BUFFER_BATCH(10) 条写回。
    仅 llm_generate 模式且 LLM 已配置时生效；其他情况返回 0。
    """
    from datetime import datetime
    from sqlalchemy import or_
    from .. import models
    try:
        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
        mode = settings_map.get("participate_text_mode", "custom")
        if not _mode_uses_llm(mode):
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
        no_comment = (db.query(models.Activity)
                      .filter(models.Activity.status.in_(["pending", "participated"]),
                              models.Activity.end_time.isnot(None),
                              models.Activity.end_time > now,
                              or_(models.Activity.comment_text.is_(None),
                                  models.Activity.comment_text == ""))
                      .order_by(models.Activity.end_time.asc())
                      .limit(COMMENT_BUFFER_BATCH).all())
        if len(no_comment) > COMMENT_BUFFER_THRESHOLD:
            return 0
        # 用序号 1..N 作为 LLM 返回 id（避免长 id 被截断，映射回数据库自增 id）
        items = [{"id": str(i + 1),
                  "text": (a.desc or "") or (a.title or "")}
                 for i, a in enumerate(no_comment)]
        generated = generate_llm_comments_batch(llm_cfg, items)
        cnt = 0
        for i, a in enumerate(no_comment):
            c = generated.get(str(i + 1))
            if c:
                a.comment_text = c
                cnt += 1
        if cnt:
            db.commit()
        return cnt
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def ensure_comment_pools(db, limit: int = 8, newest_first: bool = False) -> int:
    """独立评论补齐：保证待参与活动评论池条数 = active 账号数。

    - 找出「评论池条数 != 当前账号数」的待参与活动：无评论、不足（新增账号后
      旧池只有 2 条但账号变 3 个）都会**覆盖重生成**为账号数条
    - 批量一次 LLM 请求多个活动，每活动生成 N 条（N=active 账号数），
      评论贴合活动、简短、带 B 站 emoji；失败自动重试
    - newest_first=True：最新入库活动优先（扫描入库后触发用，立即给新活动生成）；
      默认按开奖时间升序（参与前触发用，保证最早参与的活动先有池）
    仅 llm_generate 模式且 LLM 已配置时生效。返回补齐的活动数。
    """
    with _pools_lock:
        return _ensure_comment_pools_locked(db, limit, newest_first)


_pools_lock = threading.Lock()


def _ensure_comment_pools_locked(db, limit: int = 8, newest_first: bool = False) -> int:
    from datetime import datetime
    from .. import models
    try:
        settings_map = {r.key: r.value for r in db.query(models.Setting).all()}
        mode = settings_map.get("participate_text_mode", "custom")
        if not _mode_uses_llm(mode):
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
        active_ids = [a.id for a in db.query(models.Account)
                      .filter_by(status="active").all()]
        need_count = len(active_ids)
        if need_count <= 0:
            return 0
        now = datetime.now()
        query = (db.query(models.Activity)
                 .filter(models.Activity.status.in_(["pending", "participated"]),
                         models.Activity.end_time.isnot(None),
                         models.Activity.end_time > now))
        if newest_first:
            # 最新入库优先（扫描入库后触发：立即给新活动生成评论池）
            cands = query.order_by(models.Activity.created_at.desc()).limit(200).all()
        else:
            # 开奖时间升序（参与前触发：最早参与的活动先有池）
            cands = query.order_by(models.Activity.end_time.asc()).limit(200).all()
        # 只补「还有账号未参与」的活动——已参与完的没人会再参与，不浪费 LLM 调用；
        # 评论池条数 != 账号数 的活动才需要生成/覆盖（新增账号后旧池不足也覆盖）。
        import json as _json
        need = []
        for a in cands:
            if len(_parse_comment_pool(a.comment_text or "")) == need_count:
                continue
            try:
                accs = _json.loads(a.participated_accounts or "[]")
                if not isinstance(accs, list):
                    accs = []
            except Exception:
                accs = []
            # 全部 active 账号都已参与 -> 跳过（不会再参与，不用补池）
            if accs and all(aid in accs for aid in active_ids):
                continue
            need.append(a)
            if len(need) >= limit:
                break
        if not need:
            return 0
        items = [{"id": str(a.id), "text": ((a.desc or "") or (a.title or ""))[:1200]}
                 for a in need]
        generated = generate_comment_pools_batch(llm_cfg, items, need_count)
        cnt = 0
        import json as _json
        for a in need:
            pool = generated.get(str(a.id))
            if pool:
                if mode == "random":
                    # random 混合模式：池里掺入自定义文案池短评（最后一条换掉），
                    # 避免预生成池全为 LLM 评论导致"LLM/自定义"混合失效；
                    # 参与时按账号从池取，天然混出不同来源的评论
                    pool = list(pool)
                    if len(pool) > 1:
                        try:
                            _cust_row = db.query(models.Setting).filter_by(
                                key="participate_text").first()
                            pool[-1] = pick_custom_pool_comment(
                                _cust_row.value if _cust_row else "")
                        except Exception:
                            pass
                        random.shuffle(pool)
                a.comment_text = _json.dumps(pool, ensure_ascii=False)
                cnt += 1
        if cnt:
            db.commit()
        return cnt
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def resolve_participate_text(    *,
    mode: str,
    custom_text: str,
    fallback_text: str,
    client: bili_client.BiliClient,
    dynamic_id: str,
    activity_text: str = "",
    llm_cfg: dict | None = None,
    allow_network: bool = True,
) -> dict:
    """解析参与文案，返回 {text, source, pool_size?, generated?}

    mode:
      custom          -> 从自定义文案池（participate_text 多行）随机挑一条
      llm_generate    -> LLM 生成贴合正文的评论（失败回退自定义文案池）
      random          -> 随机混合：从【LLM 生成 / 自定义文案池】中随机挑一种
                         策略，失败自动降级到自定义文案池
    allow_network=False 时跳过 LLM（纯本地，用于批量预生成测试）
    """
    text = ""
    source = "custom"
    extra = {}

    if mode == "random":
        # 随机混合：打乱策略顺序逐个尝试（LLM 贴合评论 / 自定义文案池）
        strategies = []
        if allow_network and llm_cfg and llm_cfg.get("base_url") and llm_cfg.get("model"):
            strategies.append("llm_generate")
        strategies.append("template")
        random.shuffle(strategies)
        for s in strategies:
            if s == "llm_generate":
                generated = generate_llm_comment(llm_cfg, activity_text)
                extra["generated"] = True
                if generated:
                    text = generated
                    source = "llm_generate"
                    break
            else:
                text = pick_custom_pool_comment(custom_text)
                source = "template"
                if text:
                    break

    if mode == "llm_generate" and allow_network and llm_cfg:
        generated = generate_llm_comment(llm_cfg, activity_text)
        extra["generated"] = True
        if generated:
            text = generated
            source = "llm_generate"

    if not text:
        # 兜底：自定义文案池（多行随机挑一条）→ 调用方兜底文案
        template = pick_custom_pool_comment(custom_text)
        text = (template or fallback_text or "").strip()
        if source != "custom":
            source = "custom_fallback"
    return {"text": text[:MAX_TEXT_LEN], "source": source, **extra}
