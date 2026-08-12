"""数据库连接与会话管理（SQLite + SQLAlchemy）"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# 数据目录：exe/Docker 运行时可通过环境变量 BILI_DATA_DIR 指定（exe 旁 data/、容器挂载卷），
# 否则默认项目 backend/data
_DATA_OVERRIDE = os.environ.get("BILI_DATA_DIR")
if _DATA_OVERRIDE:
    DATA_DIR = _DATA_OVERRIDE
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bili_lottery.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
