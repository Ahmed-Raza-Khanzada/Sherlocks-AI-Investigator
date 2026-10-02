from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import create_engine

Base = declarative_base()


class ReportJob(Base):
    __tablename__ = "report_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_type = Column(String(20), nullable=False, default="single")   # "single" | "multi"
    status = Column(String(50), nullable=False, default="pending")    # pending | processing | completed | failed
    progress_pct = Column(Integer, nullable=False, default=0)
    message = Column(String(500), nullable=True)
    # Single-CDR: PDF path stored here. Multi-CDR: PDF path stored here too.
    result_file_path = Column(String(500), nullable=True)
    # Multi-CDR only: Excel workbook path
    result_excel_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# Configure Database Connection
# DATABASE_URL is provided via .env / docker-compose; fallback is for local dev only
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://cdr_user:cdr_pass@localhost:5432/cdr_jobs")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_recycle=3600,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency generator for FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initializes tables (usually replaced by alembic in production, but fine for quick init)."""
    Base.metadata.create_all(bind=engine)
