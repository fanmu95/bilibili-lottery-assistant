"""Pydantic 请求/响应模型"""
from typing import List, Optional

from pydantic import BaseModel


class QRPollRequest(BaseModel):
    qrcode_key: str


class QRGenRequest(BaseModel):
    """生成登录二维码。account_id 用于「重登」——登录成功后直接更新该账号，
    即使扫码的 uid 与原账号不同也不会新建账号。"""
    account_id: Optional[int] = None


class AccountUpdate(BaseModel):
    note: Optional[str] = None
    status: Optional[str] = None


class MonitorUserCreate(BaseModel):
    uid: str
    monitor_type: str = "repost"
    note: Optional[str] = ""


class MonitorUserBatchCreate(BaseModel):
    uids: List[str]
    monitor_type: str = "repost"
    note: Optional[str] = ""


class MonitorUserUpdate(BaseModel):
    monitor_type: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None


class ActivityCreate(BaseModel):
    title: str
    link: Optional[str] = ""
    desc: Optional[str] = ""
    author_name: Optional[str] = ""
    prize_info: Optional[str] = ""
    winner_count: Optional[int] = 0
    status: str = "pending"
    end_time: Optional[str] = None  # ISO 字符串，开奖/结束时间
    note: Optional[str] = ""


class ActivityUpdate(BaseModel):
    title: Optional[str] = None
    desc: Optional[str] = None
    link: Optional[str] = None
    author_name: Optional[str] = None
    prize_info: Optional[str] = None
    winner_count: Optional[int] = None
    status: Optional[str] = None
    end_time: Optional[str] = None
    note: Optional[str] = None


class ScanRequest(BaseModel):
    user_ids: Optional[List[int]] = None  # None = 扫描全部
    reset: bool = False                    # True = 清空断点重扫全部


class ParticipateRequest(BaseModel):
    account_id: Optional[int] = None  # 使用哪个账号参与；默认第一个已登录账号


class BatchIdsRequest(BaseModel):
    ids: List[int]


class BatchParticipateRequest(BaseModel):
    activity_ids: List[int]
    account_id: Optional[int] = None


class SettingsUpdate(BaseModel):
    settings: dict


class LLMConfigRequest(BaseModel):
    base_url: str
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 1024
    top_p: float = 1.0
    system_prompt: str = ""
    message: str = "你好，请回复：连接成功"
