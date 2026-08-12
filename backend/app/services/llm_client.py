"""LLM 客户端：OpenAI 兼容接口（模型列表 / 对话 / 抽奖活动解析）

支持：
- 文本解析（有正文的动态）
- 图片解析（纯图片动态，base64 传给 vision 模型）
- 并行调用由调用方使用 ThreadPoolExecutor；本模块保证单次调用幂等
- 429 风控自动重试（指数退避 + 随机抖动）
"""
import json
import logging
import random
import re
import time
from datetime import date, datetime

import requests

logger = logging.getLogger("bili.lottery.llm")

# 抽奖判定标准（单条/批量共用）：明确"什么才算抽奖"与"哪些不算"
PARSE_LOTTERY_JUDGE = (
    "【抽奖判定】is_lottery=true 需同时满足：\n"
    "①有互动参与要求（转发/关注/评论/点赞/收藏/三连/@好友等）；\n"
    "②有奖品描述（哪怕「惊喜好礼」「周边」「小礼物」「福利」等模糊词也算）；\n"
    "③以随机抽取方式送出（抽/揪/抽送/随机/抽奖等字眼）。\n"
    "以下情况 is_lottery=false：开奖公示/中奖名单展示；试用/体验申请（申请试用/免费体验/领券）；"
    "抽奖活动汇总/合集/大盘点文章（标题含『合集/汇总/精选』且罗列多个抽奖、多个开奖日期——"
    "只是收集展示他人抽奖的导航，本身不可参与）；"
    "无随机抽取的先到先得福利（打卡/问卷/投票/前N名）；"
    "纯广告或普通晒单。\n"
)

# 字段提取说明（单条/批量共用）：精炼标题、合并多档奖品、人数求和、时间防误判
PARSE_FIELDS_GUIDE = (
    "字段含义：\n"
    "- is_lottery: 这条动态是否为抽奖/福利活动（按上方判定标准）\n"
    "- title: 精炼的活动标题（品牌/主题+活动，20 字内），"
    "去掉话题标签（#XX#）、emoji 和多余符号\n"
    "- prize: 奖品。提取正文中「送出/抽/揪/赠送/解锁/获得/好礼/奖品」等后面跟的奖品内容："
    "①多个档位奖品合并输出，用「；」分隔（如「一等奖：手机×1；二等奖：耳机×3」或「手机；耳机」）；"
    "②描述模糊（「惊喜好礼」「周边」「小礼物」「福利」）也要原样输出，不要留空；"
    "③完全没有提到任何奖品才输出空字符串\n"
    "- winner_count: 中奖总人数（数字）。多个档位人数相加"
    "（如「抽2位送A、抽3位送B」→ 5；「一等奖1人、二等奖2人」→ 3）；无法确定则为 0\n"
    "- end_time: 开奖或参与截止时间，输出 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。"
    "推算规则：①正文有明确开奖/截止/抽奖日期（含年月日/月日）→ 直接输出该日期；"
    "缺年份一律按**当年**补全（今天是 2026 年，「8月7日」→ 2026-08-07，"
    "即使该日期已过也按当年，**绝不擅自推断到明年**）；"
    "②正文只有相对时间（如「下周」「本周」「月底」「N天后」「周五」「明晚」等）→ "
    "结合当前日期（含星期几，会在输入末尾提供）推算出具体公历日期，不要留空；"
    "无法精确到具体某天时给出合理推断（如「下周抽」→ 推算为下周某天，取较近的合理日期）；"
    "③正文完全没有提到开奖/截止/抽奖日期 → 空字符串。"
    "注意：只有「开奖/截止/抽奖/公布/中奖名单」相关时间才算；"
    "**开启预约/正式发售/预售/开售/上架/上新/直播开播/活动开始/打卡开启/抽卡池开放**等"
    "商业或活动节点时间**一律不要**当作结束时间输出\n"
    "- participate_way: 参与方式完整提取（如「转发+关注+评论并@好友」）；没有则空字符串\n"
    "- desc: 完整保留动态原文\n"
    "若 is_lottery 为 false，其余字段留空即可。"
)

# 解析抽奖活动用的系统提示词（要求输出严格 JSON）
# 设计原则：理解驱动而非规则驱动——不框死模型找固定关键词/固定句式，
# 而是让模型读懂每条动态的差异化表述，自行判断与提取。
# 注意：这条提示词会同步写入设置项 llm_system_prompt / llm_model_overrides，
# 修改后请同步更新数据库，保证设置页展示与实际生效一致。
PARSE_SYSTEM_PROMPT = (
    "你是哔哩哔哩抽奖活动解析助手。请仔细阅读用户提供的 B 站动态内容，"
    "理解这条动态在说什么，判断它是否属于抽奖/福利活动，并提取相关信息。\n"
    "动态表述多种多样，请依据内容自行判断、自行理解，"
    "不要只按固定词句匹配。\n"
    + PARSE_LOTTERY_JUDGE
    + "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"is_lottery": true, "title": "活动标题", "prize": "奖品", '
    '"winner_count": 1, "end_time": "2026-08-07", '
    '"participate_way": "参与方式", "desc": "动态原文"}\n'
    + PARSE_FIELDS_GUIDE
)


# 批量解析用的系统提示词：一次请求解析多条动态，输出 JSON 数组。
# 设计原则与 PARSE_SYSTEM_PROMPT 一致（理解驱动），仅输出格式改为数组并带 id 对应。
# 扫描批量解析固定使用本提示词（保证数组输出契约）；自定义提示词用于单条/测试连接。
PARSE_BATCH_SYSTEM_PROMPT = (
    "你是哔哩哔哩抽奖活动解析助手。我会给你一组 B 站动态，"
    "每条动态都有唯一的 id。请逐条阅读、理解每条动态的内容，"
    "自行判断它是否属于抽奖/福利活动，并提取相关信息。"
    "动态表述多种多样，请依据内容自行判断、自行理解，"
    "不要只按固定词句匹配。\n"
    + PARSE_LOTTERY_JUDGE
    + "只输出一个 JSON 数组，数组元素与每条动态一一对应，不要输出任何其他文字：\n"
    '[{"id": "动态id", "is_lottery": true, "title": "活动标题", "prize": "奖品", '
    '"winner_count": 1, "end_time": "2026-08-07", "participate_way": "参与方式", '
    '"desc": "动态原文"}, {"id": "动态id2", "is_lottery": false, "title": "", '
    '"prize": "", "winner_count": 0, "end_time": "", "participate_way": "", "desc": ""}]\n'
    "- id: 必须与输入中对应动态的 id 完全一致（用于逐条对应）\n"
    + PARSE_FIELDS_GUIDE
)


# ---- 第二阶段：复核/纠错（初次解析后异步执行，提升奖品/结束时间准确率）----
# 输入：动态原文 + 机器初判结果；要求逐项核查纠错，正文无依据的字段必须输出空（不保留猜测）。
PARSE_REVIEW_PROMPT = (
    "你是哔哩哔哩抽奖活动解析复核助手。下面给出每条动态的**原文**和机器**初判结果**。"
    "请仔细阅读原文，逐项核查初判结果，纠正错误、补全遗漏。要求：\n"
    "①end_time：只依据原文明确提到的开奖/截止/抽奖时间。"
    "原文写明了具体时刻（如「下午6点」「18：00」「六点整」）→ 必须带时分（HH:MM）；"
    "原文只有日期没有时刻 → 只输出日期（YYYY-MM-DD），**不要编造或保留 00:00 之外的时刻**；"
    "原文完全没有提到开奖/截止/抽奖时间 → 空字符串"
    "（**开启预约/正式发售/预售/开售/上架/上新/直播开播/活动开始/打卡开启**等"
    "商业节点不算）；"
    "缺年份的日期按**当年**补全（今天是 2026 年，「8月7日」→ 2026-08-07），"
    "**不要沿用初判的错误年份**——若初判年份在正文中无依据（如初判 2027-08-07 "
    "但原文只有「8月7日」），必须纠正为当年；\n"
    "②prize：提取原文「送出/抽/揪/赠送/解锁/获得/好礼」等后的奖品，多档合并用「；」分隔；"
    "模糊词（惊喜好礼/周边/小礼物）也原样输出；原文无奖品 → 空字符串；\n"
    "③winner_count：中奖总人数，多档求和（「抽2位送A、抽3位送B」→5）；无法确定 → 0；\n"
    "④title：精炼标题（20字内），去话题标签#XX#和 emoji；\n"
    "⑤is_lottery：**抽奖合集/汇总/导航类文章 → false**——"
    "标题含『合集/汇总/大盘点/精选』且正文**罗列多个抽奖**"
    "（『置顶抽奖①/②/③』『开奖日期』多次出现、汇总多个UP主的抽奖）时，"
    "只是收集展示他人抽奖的导航文章，本身不是可参与的抽奖，"
    "即使正文含具体抽奖信息也不判 true；"
    "开奖公示/试用申请/先到先得福利（打卡/问卷/投票）→ false；\n"
    "⑥**正文没有依据的字段必须输出空字符串**，不要保留或猜测初判值；desc 字段无需输出。\n"
    "只输出一个 JSON 数组，与输入一一对应，不要输出任何其他文字：\n"
    '[{"id": "动态id", "is_lottery": true, "title": "活动标题", "prize": "奖品", '
    '"winner_count": 1, "end_time": "2026-08-07 18:00", "participate_way": "参与方式"}]'
)


def review_parse_verdicts_batch(base_url: str, api_key: str, model: str,
                                items: list, batch_size: int = 5,
                                workers: int = 3,
                                temperature=None, top_p=None,
                                max_tokens=None) -> dict:
    """第二阶段复核：对初判结果逐条核查纠错（异步执行）。

    items: [{"id": str, "text": 动态原文, "verdict": {初判 dict}}, ...]
    返回: {真实id: 复核后的 verdict dict}（仅含复核明确输出的字段）；
    复核失败（请求异常/JSON 缺失）的条目不包含。
    批间并行（ThreadPoolExecutor，默认 3 线程，与扫描初判相同策略）——
    复核 15 条从串行约 1 分钟提速到约 20 秒。
    """
    if not items or not base_url or not model:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    def _process(batch):
        """复核单批（批内序号 1..N），返回 LLM 原始 JSON 数组；失败返回 []"""
        lines = []
        for j, it in enumerate(batch, 1):
            v = it.get("verdict") or {}
            lines.append(
                f"【动态 {j}】id={j}\n原文：\n{(it.get('text') or '')[:1500]}\n"
                f"初判：is_lottery={v.get('is_lottery')} title={v.get('title')!r} "
                f"prize={v.get('prize')!r} winner_count={v.get('winner_count')} "
                f"end_time={v.get('end_time')!r}")
        try:
            reply = chat(
                base_url, api_key, model,
                [{"role": "system", "content": PARSE_REVIEW_PROMPT},
                 {"role": "user", "content": "\n\n".join(lines)}],
                temperature=temperature if temperature is not None else 0.1,
                top_p=top_p if top_p is not None else 1.0,
                max_tokens=max_tokens,
                extra_body={"chat_template_kwargs": {"thinking": True}})
            arr = _extract_json_array(reply)
            return arr if arr else []
        except Exception:
            return []

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    mapped = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_process, batches))
    # 各批按批内序号 1..N 回映射到真实 id
    for batch, arr in zip(batches, results):
        by_idx = {}
        for x in arr:
            if isinstance(x, dict) and x.get("id") is not None:
                by_idx[str(x["id"])] = x
        for j, it in enumerate(batch, 1):
            r = by_idx.get(str(j))
            if r:
                mapped[it["id"]] = r
    return mapped


def list_models(base_url: str, api_key: str = "") -> list:
    """获取模型列表，返回 [{"id": ..., "owned_by": ...}]"""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    models = data.get("data", [])
    return [{"id": m.get("id", ""), "owned_by": m.get("owned_by", "")} for m in models]


# ---- 模型输出上限自动解析 ----
# 不同服务商/模型支持的最大输出 token 不同；按模型名关键词匹配取上限，
# 未匹配用保守默认 16384。调用方不传 max_tokens 时自动取上限（"最大化"）。
MODEL_MAX_TOKENS = [
    (("deepseek", "sensenova", "v4-flash"), 65536),   # sensenova 系（deepseek-v4-flash 等）
    (("glm", "chatglm", "zhipu"), 32768),
    (("moonshot", "kimi"), 32768),
    (("qwen", "通义"), 8192),
    (("claude",), 65536),
    (("gpt-4o", "gpt-4"), 16384),
    (("gpt-3.5",), 4096),
]
DEFAULT_MAX_TOKENS = 16384


def resolve_max_tokens(model: str) -> int:
    """按模型名自动解析最大输出 token（含思考预算，防 content 截断）"""
    m = (model or "").lower()
    for keys, v in MODEL_MAX_TOKENS:
        if any(k in m for k in keys):
            return v
    return DEFAULT_MAX_TOKENS


def resolve_model_overrides(settings_map: dict, model: str) -> dict:
    """从 llm_model_overrides 解析当前模型的参数覆盖（temperature/top_p/max_tokens）。

    llm_model_overrides 格式：{"模型名": {"temperature": 0.5, "top_p": 1,
    "max_tokens": 16384, "system_prompt": "..."}}——前端"模型参数覆盖"编辑；
    未配置的字段返回空（调用方用场景默认值），max_tokens 覆盖自动解析值。
    """
    import json as _json
    out = {}
    if not model:
        return out
    try:
        raw = settings_map.get("llm_model_overrides") or "{}"
        ov = _json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(ov, dict):
            merged = ov.get(model) or {}
            for k in ("temperature", "top_p", "max_tokens"):
                v = merged.get(k)
                if v is not None:
                    try:
                        out[k] = float(v) if k != "max_tokens" else int(float(v))
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return out


def chat(base_url: str, api_key: str, model: str, messages: list,
         temperature: float = 0.7, max_tokens: int | None = None,
         top_p: float = 1.0,
         extra_body: dict | None = None) -> str:
    """调用 chat/completions 对话接口（带 429 自动重试）

    extra_body: 附加请求体字段（如 sensenova 关闭思考 chat_template_kwargs={"thinking": False}）
    max_tokens 不传（None）时按模型自动取上限（resolve_max_tokens）——
    不同模型输出上限不同，无需手动为每个模型调参。
    """
    if max_tokens is None:
        max_tokens = resolve_max_tokens(model)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
    }
    if extra_body:
        payload.update(extra_body)
    for attempt in range(5):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=180)
            if r.status_code == 429:
                # 风控：指数退避 + 抖动，最多重试 4 次
                wait = min(2 ** attempt + random.uniform(0, 1), 30)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            try:
                msg = data["choices"][0]["message"]
                # 部分返回 message 可能缺 content（reasoning 模型截断等），
                # 用 .get 容错；content 缺失时尝试 reasoning_content 字段（兜底）
                return msg.get("content") or msg.get("reasoning_content") or ""
            except (KeyError, IndexError, TypeError):
                return ""
        except requests.exceptions.HTTPError:
            # 4xx 客户端错误（参数/鉴权/上下文超限等）重试无意义，立即抛出；
            # 5xx 服务端错误退避重试
            if 400 <= r.status_code < 500:
                raise
            if attempt >= 4:
                raise
            wait = min(2 ** attempt + random.uniform(0, 1), 30)
            time.sleep(wait)
        except requests.exceptions.RequestException:
            # 超时/连接错误（ReadTimeout/ConnectionError 等）：退避重试
            if attempt >= 4:
                raise
            wait = min(2 ** attempt + random.uniform(0, 1), 30)
            time.sleep(wait)
    return ""


def parse_lottery_activity(base_url: str, api_key: str, model: str,
                           text: str = "", image_b64: str = "",
                           system_prompt: str = "") -> dict | None:
    """用 LLM 解析一条动态是否为抽奖活动并提取结构化字段。

    参数：
        text:      动态正文（有文本时）
        image_b64: 图片 base64（纯图片动态时，可选，模型支持 vision 时有效）
        system_prompt: 自定义提示词，为空用内置

    返回：
        解析出的 JSON dict；任何失败返回 None
    """
    try:
        sys_prompt = system_prompt or PARSE_SYSTEM_PROMPT
        user_content = []
        if text:
            # 附带当前日期（含星期几），供模型补全缺年份日期、推算"下周/月底"等相对时间
            today = date.today()
            user_content.append({"type": "text",
                                 "text": f"动态内容：\n{text[:3000]}\n\n"
                                         f"（参考信息：今天是 {today.isoformat()}（星期"
                                         f"{'一二三四五六日'[today.weekday()]}），"
                                         f"正文中缺少年份的日期请按此补全；「下周」「本周」「月底」"
                                         f"「N天后」等相对时间请结合今天推算具体公历日期。）"})
        elif image_b64:
            # 纯图片动态：文本提示 + 图片
            user_content.append({"type": "text",
                                 "text": "该动态为纯图片，请查看图片判断是否为抽奖活动。"})
        if image_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            })
        if not user_content:
            return None

        reply = chat(base_url, api_key, model, [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ], temperature=0.1)
        if not reply:
            return None
        return _extract_json(reply)
    except Exception:
        return None


def verify_lottery_with_llm(base_url: str, api_key: str, model: str,
                            text: str, system_prompt: str = "") -> dict | None:
    """兼容旧接口：仅文本判断是否抽奖（保持调用方不变）"""
    try:
        sys_prompt = system_prompt or (
            "你是哔哩哔哩抽奖活动识别助手。判断用户输入是否为抽奖活动，"
            "如果是，输出 JSON：{\"is_lottery\": true, \"prize\": \"奖品\", "
            "\"winner_count\": 0}；如果不是输出 {\"is_lottery\": false}。"
            "只输出 JSON。")
        reply = chat(base_url, api_key, model, [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text[:3000]},
        ], temperature=temperature if temperature is not None else 0.1,
        top_p=top_p if top_p is not None else 1.0, max_tokens=max_tokens)
        if not reply:
            return None
        return _extract_json(reply)
    except Exception:
        return None


def parse_lottery_activities_batch(base_url: str, api_key: str, model: str,
                                   items: list, batch_size: int = 10,
                                   temperature=None, top_p=None,
                                   max_tokens=None) -> list:
    """批量解析多条动态：一次请求返回数组，大幅减少请求次数（10 条/次）。

    参数：
        items: [{"id": str, "text": str}, ...] —— 每条动态的 id 与正文
        batch_size: 每批条数（默认 10）

    返回：
        与 items 顺序一致的结果列表；某条解析失败对应位置为 None。
        批量模式固定使用内置 PARSE_BATCH_SYSTEM_PROMPT（保证数组输出契约）。
    """
    if not items:
        return []
    results = []
    for i in range(0, len(items), batch_size):
        results.extend(_parse_batch_once(
            base_url, api_key, model, items[i:i + batch_size],
            temperature=temperature, top_p=top_p, max_tokens=max_tokens))
    return results


def _parse_batch_once(base_url: str, api_key: str, model: str, batch: list,
                       temperature=None, top_p=None, max_tokens=None) -> list:
    """解析单批（<=batch_size 条）动态，返回与该批顺序一致的结果列表

    开启思考模式（thinking=True）提升解析准确率；正文完整送入（2000 字），
    批次已调小（LLM_BATCH=5）控制单批输出量，max_tokens 给足防 content 截断。
    """
    try:
        lines = []
        # 用序号作为 LLM 可见的 id：真实 19 位动态 id 过长，LLM 常截断导致无法映射。
        # 序号输出稳定（1..N），按位置回映射即可。
        for idx, it in enumerate(batch, 1):
            text = (it.get("text") or "")[:2000]
            lines.append(f"【动态 {idx}】id={idx}\n{text}")
            # 互动抽奖 notice 官方结构化数据作为参考注入（提升奖品/时间准确率）
            notice = it.get("notice")
            if notice:
                from . import bili_client
                prize_txt = bili_client.BiliClient.format_notice_prizes(notice)
                end_ts = notice.get("lottery_time") or 0
                extra = [f"B站官方互动抽奖数据：奖品={prize_txt!r}"]
                if end_ts:
                    try:
                        extra.append(f"开奖时间={datetime.fromtimestamp(int(end_ts)).strftime('%Y-%m-%d %H:%M')}")
                    except (TypeError, ValueError, OSError):
                        pass
                if notice.get("participants"):
                    extra.append(f"参与人数={notice.get('participants')}")
                lines.append(f"（参考：{'；'.join(extra)}）")
        user_text = "\n\n".join(lines)
        today = date.today()
        user_text += (f"\n\n（参考信息：今天是 {today.isoformat()}（星期"
                      f"{'一二三四五六日'[today.weekday()]}），"
                      "正文中缺少年份的日期请按此补全；「下周」「本周」「月底」「N天后」等"
                      "相对时间请结合今天推算具体公历日期。）")
        # 开启思考模式提升解析准确率（sensenova reasoning 模型）。
        # 服务商不支持 chat_template_kwargs 时（4xx）自动去掉该参数重试。
        extra_body = {"chat_template_kwargs": {"thinking": True}}
        try:
            reply = chat(base_url, api_key, model, [
                {"role": "system", "content": PARSE_BATCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ], temperature=0.1,
                extra_body=extra_body)
        except requests.exceptions.HTTPError as e:
            # 4xx（参数不支持等）-> 去掉思考参数重试；5xx 交给 chat() 内重试
            if 400 <= (e.response.status_code if e.response is not None else 0) < 500:
                reply = chat(base_url, api_key, model, [
                    {"role": "system", "content": PARSE_BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ], temperature=0.1)
            else:
                raise
        arr = _extract_json_array(reply)
        if not arr:
            logger.warning("LLM 批量解析: 回复中未提取到 JSON 数组, reply_len=%s, head=%s",
                           len(reply or ""), (reply or "")[:120].replace("\n", " "))
            return [None] * len(batch)
        # 按序号 id 映射回本批顺序（兼容数字/字符串 id）
        by_id = {}
        for item in arr:
            if isinstance(item, dict):
                by_id[str(item.get("id", ""))] = item
        # 双保险：序号优先，其次原始 id
        results = []
        for idx, it in enumerate(batch, 1):
            verdict = by_id.get(str(idx))
            if verdict is None:
                verdict = by_id.get(str(it.get("id", "")))
            results.append(verdict)
        return results
    except Exception as e:
        logger.warning("LLM 批量解析异常: %s: %s", type(e).__name__, str(e)[:200])
        return [None] * len(batch)


def _extract_json(reply: str) -> dict | None:
    """从模型回复中提取 JSON 对象（容忍代码块/前后缀文本）"""
    if not reply:
        return None
    # 去代码块围栏
    clean = re.sub(r"```(?:json)?", "", reply).strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(clean[start:end + 1])
    except Exception:
        pass
    # 容错：raw_decode 直接解析第一个合法对象
    try:
        obj, _ = json.JSONDecoder().raw_decode(clean[start:])
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 再容错：去掉尾逗号后重试
    try:
        fixed = re.sub(r",\s*}", "}", clean[start:end + 1])
        return json.loads(fixed)
    except Exception:
        return None


def _extract_json_array(reply: str) -> list | None:
    """从模型回复中提取 JSON 数组（批量解析用，容忍代码块/前后缀文本）。

    整体 json.loads 失败时容错降级：逐个提取 {..} 对象（跳过坏元素），
    避免 LLM 输出个别未转义引号/尾逗号导致整批丢失。
    """
    if not reply:
        return None
    clean = re.sub(r"```(?:json)?", "", reply).strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(clean[start:end + 1])
    except Exception:
        pass
    # 容错：逐个提取 JSON 对象
    objs = []
    idx = clean.find("{", start)
    decoder = json.JSONDecoder()
    while idx != -1 and idx < end:
        try:
            obj, nxt = decoder.raw_decode(clean[idx:])
            if isinstance(obj, dict):
                objs.append(obj)
            idx = clean.find("{", idx + max(nxt, 1))
        except Exception:
            idx = clean.find("{", idx + 1)
    return objs or None
