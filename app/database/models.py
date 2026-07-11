"""ORM models: Lead (pipeline state) + EventLog (audit trail).

``Lead`` holds the full lifecycle of a lead with all SLA timestamps so success
criteria are measurable. ``EventLog`` records every transition / retry / failure
for observability and audit — with PII redacted in the ``detail`` column.

NDA / retention: ``deleted_at`` + ``anonymize_lead()`` implement right-to-erasure
without losing the non-PII audit trail.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    """A single lead and its scoring/enrichment/alert state."""

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # monday item id
    name: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    inquiry_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # SLA / lifecycle timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Scoring result
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str | None] = mapped_column(String, nullable=True)      # Hot|Warm|Cold
    classification: Mapped[str | None] = mapped_column(String, nullable=True)  # distributor|end_customer
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON list of reasons

    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)

    events: Mapped[list["EventLog"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        """Plain dict for API responses (PII included — caller must auth)."""
        return {
            "id": self.id,
            "name": self.name,
            "company": self.company,
            "source": self.source,
            "industry": self.industry,
            "inquiry_type": self.inquiry_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "scored_at": self.scored_at.isoformat() if self.scored_at else None,
            "alerted_at": self.alerted_at.isoformat() if self.alerted_at else None,
            "score": self.score,
            "tier": self.tier,
            "classification": self.classification,
            "rationale": self.rationale,
            "alert_sent": self.alert_sent,
            "escalated": self.escalated,
        }


class EventLog(Base):
    """Append-only audit trail. ``detail`` MUST NOT contain PII."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    event: Mapped[str] = mapped_column(String, nullable=False)  # intake|scored|enriched|...
    status: Mapped[str] = mapped_column(String, nullable=False)  # ok|retry|failure|escalation
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # redacted / non-PII
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    lead: Mapped["Lead | None"] = relationship(back_populates="events")
