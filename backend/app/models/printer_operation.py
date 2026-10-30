"""Durable operation receipts prevent implicit replays after request failures."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterOperation(Base):
    __tablename__ = "printer_operations"
    __table_args__ = (UniqueConstraint("printer_id", "idempotency_key", name="uq_printer_operation_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), index=True)
    operation_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    request_data: Mapped[dict] = mapped_column(JSON)
    operation_schema: Mapped[dict] = mapped_column(JSON)
    receipt: Mapped[dict] = mapped_column(JSON)
    provider_task_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    process_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
