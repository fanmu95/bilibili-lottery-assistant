"""参与文案解析：自定义文案 / 借用随机评论 / LLM 生成贴合评论

对齐 bilibinggo participate-text 语义：
- custom:        使用设置项 participate_text
- random_comment: 拉取动态评论区，跳过前 5 条热评，在 5~65 条间随机借一条
- llm_generate:  用 LLM 根据活动正文生成贴合该活动的转发评论

调用方：activities.py participate / participate-triple / batch-participate
"""
import random
import threading
import time

from . import bili_client
from . import llm_client

REPLY_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"
REPLY_PAGE_SIZE = 20
REPLY_FETCH_PAGES = 3
REPLY_SKIP_HEAD = 5          # 跳过前 5 条（热评/广告）
REPLY_POOL_END_EXCLUSIVE = 65
MAX_TEXT_LEN = 100

# 内置兜底文案池：真人随手会打的自然评论（少 emoji、无机器人口号），
# 供 random 模式/兜底使用，避免「关注+转发，支持一下，谢谢！」式生硬文案。
FALLBACK_COMMENT_POOL = [
    "蹲一个，这个看起来真不错",
    "哇这个可以啊，支持一下",
    "质感看着挺好的，关注了",
    "这波福利诚意满满，冲",
    "来了来了，支持一波",
    "看着有点心动，先留个脚印",
    "正好最近想入手，蹲一个",
    "颜值在线，属实是爱了",
    "路过支持一下，祝活动顺利",
    "这个真不错，先关注了",
    "好家伙，这福利可以的",
    "围观群众路过，支持",
    "质量看着不错，支持一下",
    "好东西要分享，转给朋友看看",
    "最近正缺这个，来碰碰运气",
    "支持一下，做得挺好",
    "这个可以有，关注了",
    "感觉挺用心的，支持",
    "不错不错，观望一下",
    "看着挺香的，蹲个结果",
    "这波操作可以，点赞",
    "来得早不如来得巧，支持",
    "心动了，蹲一个",
    "支持支持，等后续",
]


def pick_fallback_comment() -> str:
    """从内置兜底文案池随机取一条"""
    return random.choice(FALLBACK_COMMENT_POOL)


# LLM 生成评论的提示词（理解驱动：简短随意，去 AI 味）
# 注意：sensenova 是 reasoning 模型，复杂提示词会先长思考再输出，
# 思考过长会占满 max_tokens 导致 content 截断——要求"直接输出，不要思考过程"。
LLM_COMMENT_PROMPT = (
    "你是哔哩哔哩普通用户，正在刷到一条抽奖/福利动态，随手留条评论。"
    "直接输出一条 8~25 字的评论，不要输出任何思考过程、解释或前缀。要求：\n"
    "①要贴合这条动态——提到奖品、作品/游戏名、角色、话题等具体信息，"
    "让人觉得你真看过内容，不是复制粘贴；\n"
    "②像真人随手打的：口语化、随意，可以有语气词、网络梗，甚至带点调侃；\n"
    "③不要堆表情：最多用一个常见表情（😂😆👍🥰🔥 等），能不用就不用，"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型表情，真人不会那么发；\n"
    "④千万不要说「已转发关注三连」「希望能中奖」「坐等开奖」「求好运」"
    "「祝自己」这类机器人口号，也不要说「我真的很想要」这种客套话；\n"
    "⑤只输出评论本身，不要引号。"
)


def _extract_comment_id_and_type(detail: dict) -> tuple[str, int]:
    """从动态详情提取评论所需的 oid 与 comment_type（对齐 bilibinggo）"""
    basic = detail.get("basic") or {}
    if not isinstance(basic, dict):
        basic = {}
    rid = str(basic.get("comment_id_str") or detail.get("id_str") or "")
    ctype = int(basic.get("comment_type") or 17)
    return rid, ctype


def fetch_reply_messages(client: bili_client.BiliClient, dynamic_id: str,
                         pages: int = REPLY_FETCH_PAGES,
                         page_size: int = REPLY_PAGE_SIZE) -> list[str]:
    """拉取动态评论区内容。

    实测：匿名只返回 3 条热评，登录态才返回完整评论（20 条/页）——
    借用评论本就是参与账号身份操作，用登录 session。
    """
    detail = client.get_dynamic_detail(dynamic_id)
    if not detail:
        return []
    rid, ctype = _extract_comment_id_and_type(detail)
    if not rid:
        return []
    messages = []
    next_cursor = 0
    for _ in range(max(1, pages)):
        try:
            r = client.session.get(
                REPLY_MAIN_URL,
                params={"oid": rid, "type": ctype, "mode": 2,
                        "next": next_cursor, "ps": page_size},
                headers={"Referer": f"https://www.bilibili.com/opus/{dynamic_id}"},
                timeout=12)
            d = r.json()
            if d.get("code") != 0:
                break
            data = d.get("data") or {}
            replies = data.get("replies") or []
            if isinstance(replies, list):
                for reply in replies:
                    content = reply.get("content") or ""
                    if isinstance(content, dict):
                        msg = str(content.get("message") or "")
                    else:
                        # B 站评论 content 可能是纯文本（非 dict）
                        msg = str(content)
                    if msg:
                        messages.append(msg)
            cursor = data.get("cursor") or {}
            if not isinstance(cursor, dict) or cursor.get("is_end"):
                break
            next_raw = cursor.get("next")
            if next_raw is None:
                break
            next_cursor = int(next_raw)
        except Exception:
            break
    return messages


def pick_random_comment(messages: list[str]) -> str | None:
    """在评论池（跳过前 5 条热评）中随机借一条"""
    pool = messages[REPLY_SKIP_HEAD:REPLY_POOL_END_EXCLUSIVE]
    if not pool:
        return None
    return random.choice(pool)


def fetch_reply_users(client: bili_client.BiliClient, dynamic_id: str,
                      pages: int = REPLY_FETCH_PAGES,
                      page_size: int = REPLY_PAGE_SIZE) -> list[dict]:
    """拉取动态评论区用户（职业抽奖号发现用）：[{uid, uname, message}]"""
    detail = client.get_dynamic_detail(dynamic_id)
    if not detail:
        return []
    rid, ctype = _extract_comment_id_and_type(detail)
    if not rid:
        return []
    users = []
    seen_uids = set()
    next_cursor = 0
    for _ in range(max(1, pages)):
        try:
            r = client.session.get(
                REPLY_MAIN_URL,
                params={"oid": rid, "type": ctype, "mode": 2,
                        "next": next_cursor, "ps": page_size},
                headers={"Referer": f"https://www.bilibili.com/opus/{dynamic_id}"},
                timeout=12)
            d = r.json()
            if d.get("code") != 0:
                break
            data = d.get("data") or {}
            replies = data.get("replies") or []
            if isinstance(replies, list):
                for reply in replies:
                    member = reply.get("member") or {}
                    uid = str(member.get("mid") or "")
                    if not uid or uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    content = reply.get("content") or ""
                    if isinstance(content, dict):
                        msg = str(content.get("message") or "")
                    else:
                        msg = str(content)
                    users.append({
                        "uid": uid,
                        "uname": member.get("uname", "") or uid,
                        "message": msg[:100],
                    })
            cursor = data.get("cursor") or {}
            if not isinstance(cursor, dict) or cursor.get("is_end"):
                break
            next_raw = cursor.get("next")
            if next_raw is None:
                break
            next_cursor = int(next_raw)
        except Exception:
            break
    return users


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
            temperature=0.9, max_tokens=16384,
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
                temperature=0.9, max_tokens=16384)
            text = (reply or "").strip().strip('"\'「」')
            return text[:MAX_TEXT_LEN] if text else None
        except Exception:
            return None


BATCH_COMMENT_PROMPT = (
    "你是哔哩哔哩普通用户，正在刷到多条抽奖/福利动态，随手留评论。"
    "直接输出 JSON，不要输出任何思考过程、解释或前缀。对每条动态写一条 8~25 字的评论。要求：\n"
    "①贴合该条动态——提到奖品、作品/游戏名、角色、话题等具体信息，"
    "让人觉得你真看过内容；\n"
    "②像真人随手打的：口语化、随意，可以有语气词、网络梗，甚至带点调侃；\n"
    "③不要堆表情：最多用一个常见表情（😂😆👍🥰🔥 等），能不用就不用，"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型表情；\n"
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
                temperature=0.9, max_tokens=16384,
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
    "3. 不要堆表情：最多用一个常见表情（😂😆👍🥰🔥 等），能不用就不用，"
    "绝不要用「🎉✨🎁💖🥳」这类活动/庆祝型表情\n"
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
                    temperature=0.9, max_tokens=16384,
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
        f"为这条动态写 {count} 条**内容各不相同**的评论，每条 8~25 字。要求：\n"
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
            temperature=0.9, max_tokens=16384,
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
      custom          -> 设置的自定义文案
      random_comment  -> 评论区随机借一条（失败回退兜底文案池）
      llm_generate    -> LLM 生成贴合正文的评论（失败回退兜底文案池）
      random          -> 随机混合：从【真实评论 / LLM 生成 / 内置兜底文案】中
                         随机挑一种策略，失败自动降级到兜底文案池
    allow_network=False 时跳过评论拉取/LLM（纯本地，用于批量预生成测试）
    """
    text = ""
    source = "custom"
    extra = {}

    if mode == "random":
        # 随机混合：打乱策略顺序逐个尝试（真实评论 / LLM / 兜底文案）
        strategies = []
        if allow_network and client:
            strategies.append("random_comment")
        if allow_network and llm_cfg and llm_cfg.get("base_url") and llm_cfg.get("model"):
            strategies.append("llm_generate")
        strategies.append("template")
        random.shuffle(strategies)
        for s in strategies:
            if s == "random_comment":
                try:
                    messages = fetch_reply_messages(client, dynamic_id)
                    picked = pick_random_comment(messages)
                    extra["pool_size"] = max(0, len(messages) - REPLY_SKIP_HEAD)
                    if picked:
                        text = picked[:MAX_TEXT_LEN]
                        source = "random_comment"
                        break
                except Exception:
                    pass
            elif s == "llm_generate":
                generated = generate_llm_comment(llm_cfg, activity_text)
                extra["generated"] = True
                if generated:
                    text = generated
                    source = "llm_generate"
                    break
            else:
                text = pick_fallback_comment()
                source = "template"
                break

    if mode == "random_comment" and allow_network and client:
        try:
            messages = fetch_reply_messages(client, dynamic_id)
            picked = pick_random_comment(messages)
            extra["pool_size"] = max(0, len(messages) - REPLY_SKIP_HEAD)
            if picked:
                text = picked[:MAX_TEXT_LEN]
                source = "random_comment"
        except Exception:
            pass

    if mode == "llm_generate" and allow_network and llm_cfg:
        generated = generate_llm_comment(llm_cfg, activity_text)
        extra["generated"] = True
        if generated:
            text = generated
            source = "llm_generate"

    if not text:
        # 兜底：优先内置自然文案池（比"关注+转发，支持一下"生硬文案真实），
        # 自定义文案存在时仍优先自定义。
        template = pick_fallback_comment()
        text = (custom_text or template or fallback_text or "").strip()
        if source != "custom":
            source = "custom_fallback"
    return {"text": text[:MAX_TEXT_LEN], "source": source, **extra}
