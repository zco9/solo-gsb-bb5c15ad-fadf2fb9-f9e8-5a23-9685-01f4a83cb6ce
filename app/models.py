"""分析记录的 ORM 模型。"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Analysis(Base):
    """一次 JSON Schema 兼容性分析的完整快照。"""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    old_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    new_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    samples: Mapped[list | None] = mapped_column(JSON, nullable=True)
    report: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    profiles: Mapped[list["AnalysisProfile"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "old_schema": self.old_schema,
            "new_schema": self.new_schema,
            "samples": self.samples or [],
            "report": self.report,
        }


class AnalysisProfile(Base):
    """对一次分析的新 schema 执行文档合规画像的持久化快照。"""

    __tablename__ = "analysis_profiles"
    __table_args__ = (
        # 同一分析下 version 唯一递增；通过 (analysis_id, version) 稳定寻址
        UniqueConstraint("analysis_id", "version", name="uq_profile_analysis_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    analysis: Mapped[Analysis] = relationship(back_populates="profiles")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            **self.snapshot,
        }
