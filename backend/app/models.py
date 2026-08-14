"""ORM 数据模型"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from .database import Base


class Account(Base):
    """B 站账号（多账号管理）"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String(64), unique=True, index=True)
    username = Column(String(128), default="")
    avatar = Column(String(512), default="")
    level = Column(Integer, default=0)
    vip_status = Column(Integer, default=0)
    coins = Column(Integer, default=0)
    cookies = Column(Text, default="")            # JSON 字符串
    status = Column(String(32), default="active") # active / expired
    note = Column(String(256), default="")
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MonitorUser(Base):
    """活动发现：被监控用户（抽奖活动来源）"""
    __tablename__ = "monitor_users"

    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String(64), index=True)
    username = Column(String(128), default="")
    avatar = Column(String(512), default="")
    # repost = 监控用户转发的抽奖活动；publish = 监控用户发布的抽奖活动
    monitor_type = Column(String(32), default="repost")
    status = Column(String(32), default="active")
    note = Column(String(256), default="")
    last_scanned_at = Column(DateTime, nullable=True)
    empty_scan_count = Column(Integer, default=0)  # 连续无活动扫描次数（>= 设置阈值标记失效剔除）
    scanned_count = Column(Integer, default=0)     # 累计扫描发现的活动数（质量指标）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Activity(Base):
    """识别到的抽奖活动"""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, index=True)
    activity_id = Column(String(128), index=True)  # B 站动态 id
    title = Column(String(256), default="")
    desc = Column(Text, default="")
    link = Column(String(512), default="")
    author_uid = Column(String(64), default="")
    author_name = Column(String(128), default="")
    source_uid = Column(String(64), default="")
    source_name = Column(String(128), default="")
    source_type = Column(String(32), default="repost")  # repost / publish
    prize_info = Column(Text, default="")
    winner_count = Column(Integer, default=0)
    repost_count = Column(Integer, default=0)
    publish_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)  # 开奖/结束时间
    comment_text = Column(Text, default="")  # 参与文案（解析时预生成/参与时生成后缓存）
    # pending 待参与 / participated 已参与 / skipped 已跳过 / failed 参与失败 / ended 已结束
    # （中奖结果由用户自行判断，系统不维护 won 状态）
    status = Column(String(32), default="pending")
    note = Column(String(256), default="")
    participated_at = Column(DateTime, nullable=True)
    participated_accounts = Column(Text, default="[]")  # JSON: 参与该活动的账号 id 列表
    reviewed_at = Column(DateTime, nullable=True)  # 两阶段复核：第二次 LLM 评判纠错完成时间
    pro_discovered_at = Column(DateTime, nullable=True)  # 职业号发现完成时间（避免重复发现）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Log(Base):
    """运行日志"""
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(16), default="info")  # info / success / warning / error
    module = Column(String(64), default="system")
    message = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)


class Setting(Base):
    """键值配置"""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, index=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
