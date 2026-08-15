"""Local SQLite persistence.

Everything stays on the machine this runs on. No tax document ever leaves it,
which is the whole point of preparing a return locally rather than handing the
Form 16 to a website.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

from .schemas import TaxReturn

DATA_DIR = Path(os.environ.get("ITR_DATA_DIR", Path.home() / ".india-itr"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "returns.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


class ReturnRecord(Base):
    __tablename__ = "returns"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    assessment_year = Column(String(10), default="2026-27")
    label = Column(String(120), default="")
    payload = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = relationship(
        "DocumentRecord", back_populates="return_record",
        cascade="all, delete-orphan",
    )

    def load(self) -> TaxReturn:
        try:
            return TaxReturn.model_validate(json.loads(self.payload or "{}"))
        except Exception:  # noqa: BLE001 - a corrupt row must not brick the app
            return TaxReturn(assessment_year=self.assessment_year)

    def save(self, tax_return: TaxReturn) -> None:
        self.payload = tax_return.model_dump_json()
        self.assessment_year = tax_return.assessment_year


class DocumentRecord(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    return_id = Column(String(36), ForeignKey("returns.id", ondelete="CASCADE"))
    filename = Column(String(255))
    document_type = Column(String(40), default="unknown")
    confidence = Column(String(10), default="0")
    extraction = Column(Text, default="{}")
    applied = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    return_record = relationship("ReturnRecord", back_populates="documents")

    def load_extraction(self) -> Dict[str, Any]:
        try:
            return json.loads(self.extraction or "{}")
        except json.JSONDecodeError:
            return {}


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()


def get_or_create_return(
    session: Session, return_id: Optional[str], assessment_year: str = "2026-27"
) -> ReturnRecord:
    if return_id:
        record = session.get(ReturnRecord, return_id)
        if record:
            return record
    record = ReturnRecord(assessment_year=assessment_year)
    record.save(TaxReturn(assessment_year=assessment_year))
    session.add(record)
    session.commit()
    return record


def list_returns(session: Session) -> List[ReturnRecord]:
    return (
        session.query(ReturnRecord)
        .order_by(ReturnRecord.updated_at.desc())
        .all()
    )
