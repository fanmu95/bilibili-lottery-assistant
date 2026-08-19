"""设置：LLM 配置 / 模型列表 / 输入输出参数 / 扫描与参与参数

对齐 bilibinggo 契约：
  GET  /api/settings        设置展示
  GET  /api/settings/llm    LLM 面板（脱敏）
  POST /api/settings/llm    保存配置
  POST /api/settings/llm/test  测试连接
增强：获取模型列表、按模型维度保存输入输出参数（temperature/max_tokens/top_p/系统提示词）。
"""
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas
from ..services import llm_client
from .logs import add_log

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULT_SETTINGS = {
    # ---- LLM ----
    "llm_enabled": False,
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_api_key": "",
    "llm_model": "",
    "llm_temperature": 0.7,
    "llm_max_tokens": 1024,
    "llm_top_p": 1.0,
    "llm_system_prompt": (
        "你是哔哩哔哩抽奖活动识别助手。判断用户输入是否为抽奖活动，"
        "如果是，输出 JSON：{\"is_lottery\": true, \"prize\": \"奖品\", \"winner_count\": 0}；"
        "如果不是输出 {\"is_lottery\": false}。只输出 JSON。"),
    "llm_model_overrides": "{}",             # {model: {temperature,max_tokens,top_p,system_prompt}}
    # ---- 参与文案（对齐 bilibinggo participate-text）----
    # 自定义文案池：多行文本，一行一条评论（custom/random 模式从此随机挑，可随时编辑热更新）
    "participate_text": (
        "蹲一个，这个看起来真不错\n"
        "哇这个可以啊，支持一下 [打call]\n"
        "质感看着挺好的，关注了\n"
        "这波福利诚意满满，冲 [doge]\n"
        "来了来了，支持一波\n"
        "看着有点心动，先留个脚印 [星星眼]\n"
        "正好最近想入手，蹲一个\n"
        "颜值在线，属实是爱了 [喜欢]\n"
        "路过支持一下，祝活动顺利\n"
        "这个真不错，先关注了\n"
        "好家伙，这福利可以的 [妙啊]\n"
        "围观群众路过，支持 [吃瓜]\n"
        "质量看着不错，支持一下\n"
        "好东西要分享，转给朋友看看\n"
        "最近正缺这个，来碰碰运气\n"
        "支持一下，做得挺好\n"
        "这个可以有，关注了\n"
        "感觉挺用心的，支持\n"
        "不错不错，观望一下\n"
        "看着挺香的，蹲个结果\n"
        "这波操作可以，点赞 [OK]\n"
        "来得早不如来得巧，支持\n"
        "心动了，蹲一个 [doge]\n"
        "支持支持，等后续\n"
        "好运连连\n"
        "抽我\n"
        "好运\n"
        "蹲\n"
        "冲\n"
        "羡慕了\n"
        "想要\n"
        "排一个\n"
        "带带我\n"
        "沾沾喜气\n"
        "锦鲤附体\n"
        "好运加持\n"
        "试试手气\n"
        "随缘\n"
        "前排占座\n"
        "凑个热闹\n"
        "666\n"
        "爱了爱了\n"
        "欧皇保佑\n"
        "抽中我\n"
        "选我选我\n"
        "就决定是我了\n"
        "蹭蹭欧气\n"
        "坐等欧气\n"
        "好运常伴\n"
        "冲鸭\n"
        "占楼\n"
        "蹲住\n"
        "码住\n"
        "碰碰运气\n"
        "来一个\n"
        "眼馋\n"
        "馋了\n"
        "沾光\n"
        "借点欧气\n"
        "祈福\n"
        "想中一次\n"
        "试试看"),
    "participate_text_mode": "custom",       # custom / llm_generate / random（随机混合）
    # 注：参与文案统一由后台线程预生成评论池（参与时秒取，无需生成时机设置）
    # ---- 扫描与调度 ----
    "scan_interval": 60,                     # 自动扫描间隔（分钟）
    "auto_scan_enabled": True,               # 活动发现页"自动扫描"开关：按 scan_interval 定时批量扫描
    "scan_llm_verify": False,                # 扫描时是否用 LLM 增强识别
    "watch_backfill_days": 10,               # 监控用户动态回溯天数（对齐 bilibinggo 默认约10天）
    "auto_schedule_enabled": False,          # 定时自动参与（对齐 auto/status）
    "auto_schedule_time": "10:00",
    "review_interval_min": 5,                # 后台复核间隔（分钟，独立于全自动轮次，后端启动常驻自动修正奖品/时间）
    "participate_batch": 3,                  # 单批次参与数量
    "skip_charge_lottery": True,             # 充电抽奖自动跳过（对齐 bilibinggo）
    "auto_pro_scan_enabled": True,           # 自动模式开关：轮次冷却期是否扫描职业号（错峰执行）
    "monitor_empty_scan_remove": 3,          # 监控用户连续 N 次扫描无活动 → 标记失效剔除（0=不启用）
    # ---- 防风控 ----
    "daily_participate_limit": 100,          # 每账号每日参与上限（防被标记）
    "action_interval_min": 1.5,              # 动作最小间隔（秒，随机抖动）
    "action_interval_max": 3.0,              # 动作最大间隔（秒）
    "activity_gap_min": 3.0,                 # 活动间最小间隔（秒，参与完一个到下一个）
    "activity_gap_max": 5.0,                 # 活动间最大间隔（秒）
    "auto_round_sleep": 60,                  # 全自动轮次间隔（秒）
    "bili_rps": 3.0,                         # 全局限流速率（请求/秒）
    # ---- 私信消息检测 ----
    "dm_check_interval_min": 30,             # 私信检测间隔（分钟，默认 30）
    "dm_check_start": "08:00",               # 检测开始时间（白名单，默认早上 08:00）
    "dm_check_end": "22:00",                 # 检测结束时间（白名单，默认晚上 22:00）
    # ---- 版本检测 ----
    "update_check_enabled": True,            # 是否检测新版本（关闭后不再提醒/展示新版本）
}


def _get_value(db: Session, key: str, default=""):
    row = db.query(models.Setting).filter_by(key=key).first()
    return row.value if row else default


def _set_value(db: Session, key: str, value):
    row = db.query(models.Setting).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))


def get_all_settings(db: Session) -> dict:
    result = dict(DEFAULT_SETTINGS)
    for row in db.query(models.Setting).all():
        result[row.key] = row.value
    return result


def _coerce(key: str, value):
    """根据默认值类型做类型转换"""
    default = DEFAULT_SETTINGS.get(key)
    if default is None:
        return value
    if isinstance(default, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except Exception:
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except Exception:
            return default
    return str(value)


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    raw = get_all_settings(db)
    out = {}
    for k, v in raw.items():
        out[k] = _coerce(k, v)
    return out


@router.get("/llm")
def get_llm_settings(db: Session = Depends(get_db)):
    """LLM 面板（api_key 脱敏）"""
    raw = get_settings(db)
    key = raw.get("llm_api_key", "")
    raw["llm_api_key"] = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ""
    return raw


@router.put("")
def save_settings(body: schemas.SettingsUpdate, db: Session = Depends(get_db)):
    for k, v in body.settings.items():
        if k not in DEFAULT_SETTINGS:
            continue
        _set_value(db, k, json.dumps(v, ensure_ascii=False)
                   if isinstance(v, (dict, list)) else str(v))
    db.commit()
    # 应用全局限流速率（防风控）
    try:
        from ..services.rate_limit import configure_rate_limit
        configure_rate_limit(float(_get_value(db, "bili_rps", 3.0)))
    except Exception:
        pass
    add_log(db, "info", "settings", "保存设置成功")
    return get_settings(db)


@router.post("/llm/models")
def fetch_llm_models(body: schemas.LLMConfigRequest,
                     db: Session = Depends(get_db)):
    """获取模型列表（OpenAI 兼容 /v1/models）"""
    try:
        models_list = llm_client.list_models(body.base_url, body.api_key)
        if not models_list:
            return {"ok": True, "models": [], "message": "未获取到模型，请检查接口地址"}
        return {"ok": True, "models": models_list}
    except Exception as e:
        return {"ok": False, "models": [], "message": f"获取模型列表失败: {e}"}


@router.post("/llm/test")
def test_llm(body: schemas.LLMConfigRequest, db: Session = Depends(get_db)):
    """测试连接：发送一条对话；同时探测模型最大输出 tokens 返回建议值"""
    suggested = None
    try:
        # ① /models 元数据探测（部分服务商直接给输出上限）
        try:
            for m in llm_client.list_models(body.base_url, body.api_key):
                if m.get("id") == body.model and m.get("max_tokens"):
                    suggested = int(m["max_tokens"])
                    break
        except Exception:
            pass
        # ② 内置模型名映射兜底（deepseek 系 65536 / glm 32768 / qwen 8192 等）
        if not suggested:
            suggested = llm_client.resolve_max_tokens(body.model)
    except Exception:
        suggested = None
    try:
        reply = llm_client.chat(
            body.base_url, body.api_key, body.model,
            [{"role": "user", "content": body.message}],
            temperature=body.temperature, max_tokens=body.max_tokens, top_p=body.top_p)
        add_log(db, "success", "settings", f"LLM 测试连接成功：{body.model}")
        return {"ok": True, "reply": reply,
                "suggested_max_tokens": suggested}
    except Exception as e:
        add_log(db, "error", "settings", f"LLM 测试连接失败：{e}")
        return {"ok": False, "message": str(e)}
