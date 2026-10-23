"""Typed contracts shared by ingestion, agents, tools, and validation.

The language model is deliberately kept behind these schemas. That gives the
rest of the application normal Python objects to validate instead of loosely
structured prose.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects unexpected fields from model responses."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Intent(str, Enum):
    """Business-level request types recognized by the first agent."""

    NEW_QUOTE_REQUEST = "new_quote_request"
    QUOTE_REVISION = "quote_revision"
    QUOTE_RELATED_OTHER = "quote_related_other"
    NOT_QUOTE = "not_quote"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    """The finite set of terminal states exposed to a human reviewer."""

    QUOTE_READY = "quote_ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    INSUFFICIENT_STOCK = "insufficient_stock"
    PRICING_REVIEW = "pricing_review"
    NOT_QUOTE = "not_quote"
    PROCESSING_ERROR = "processing_error"


class AttachmentRecord(StrictModel):
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    extracted_text: str = ""
    extraction_error: str | None = None


class InboundEmail(StrictModel):
    source_path: str
    message_id: str | None = None
    sent_at: str | None = None
    sender: str
    from_address: str
    reply_to: list[str] = Field(default_factory=list)
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    headers: dict[str, str] = Field(default_factory=dict)
    ingestion_issues: list[str] = Field(default_factory=list)
    attachments: list[AttachmentRecord] = Field(default_factory=list)

    def agent_view(self) -> dict[str, Any]:
        """Return useful email content without exposing local file paths."""

        return {
            "message_id": self.message_id,
            "sent_at": self.sent_at,
            "sender": self.sender,
            "from_address": self.from_address,
            "reply_to": self.reply_to,
            "to": self.to,
            "cc": self.cc,
            "subject": self.subject,
            "body": self.body,
            "ingestion_issues": self.ingestion_issues,
            "attachments": [
                {
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "size_bytes": item.size_bytes,
                    "extracted_text": item.extracted_text,
                    "extraction_error": item.extraction_error,
                }
                for item in self.attachments
            ],
        }


class Classification(StrictModel):
    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class RequestedLine(StrictModel):
    description: str
    quantity: float = Field(gt=0, strict=True)
    unit: str
    stock_item_id_hint: int | None = Field(default=None, gt=0, strict=True)
    evidence: str


class HandlerDraft(StrictModel):
    customer_hint: str | None = None
    required_date: str | None = None
    pricing_agreement_hints: list[str] = Field(default_factory=list)
    partial_shipment_allowed: bool | None = None
    lines: list[RequestedLine] = Field(default_factory=list)
    summary: str
    suggested_response: str


class ToolTrace(StrictModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class CustomerMatch(StrictModel):
    customer_id: int | None = None
    customer_name: str | None = None
    match_type: str = "none"
    evidence: str | None = None


class PricingAgreementMatch(StrictModel):
    agreement_id: str
    customer_id: int
    start_date: str
    end_date: str
    discount_percentage: float
    requires_review: bool
    match_type: str


class ResolvedLine(StrictModel):
    requested_description: str
    requested_quantity: float
    requested_unit: str
    stock_item_id: int | None = None
    stock_item_name: str | None = None
    match_type: str = "unresolved"
    normalized_quantity: float | None = None
    normalized_unit: str | None = None
    quantity_on_hand: int | None = None
    catalog_unit_price: float | None = None
    line_subtotal: float | None = None
    tax_rate_percent: float | None = None
    tax_amount: float | None = None
    line_total: float | None = None
    issues: list[str] = Field(default_factory=list)


class QuoteResult(StrictModel):
    outcome: Outcome
    reason_codes: list[str] = Field(default_factory=list)
    customer: CustomerMatch = Field(default_factory=CustomerMatch)
    pricing_agreements: list[PricingAgreementMatch] = Field(default_factory=list)
    lines: list[ResolvedLine] = Field(default_factory=list)
    subtotal: float | None = None
    tax_total: float | None = None
    total: float | None = None
    freight: str = "TBD"
    proposed_subject: str
    proposed_body: str
    issues: list[str] = Field(default_factory=list)


class RunRecord(StrictModel):
    run_id: str
    created_at: str
    classifier_model: str
    handler_model: str | None = None
    email: InboundEmail
    classification: Classification
    handler_draft: HandlerDraft | None = None
    tool_trace: list[ToolTrace] = Field(default_factory=list)
    validation_trace: list[ToolTrace] = Field(default_factory=list)
    result: QuoteResult
