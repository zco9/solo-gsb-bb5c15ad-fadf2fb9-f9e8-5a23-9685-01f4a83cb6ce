"""数据库初始化（SQLite + SQLAlchemy）。"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/schema_compat.db")

# check_same_thread=False：FastAPI 多线程访问同一个 SQLite 连接
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """创建 data 目录并初始化所有表。"""
    if DATABASE_URL.startswith("sqlite:///") and DATABASE_URL not in (
        "sqlite:///:memory:",
    ):
        db_path = DATABASE_URL.replace("sqlite:///", "", 1)
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
    # 触发模型注册
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
