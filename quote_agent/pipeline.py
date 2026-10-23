"""Orchestrate agents, then make the final decision with deterministic code."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from .classifier import DEFAULT_CLASSIFIER_MODEL, RequestClassifier
from .handler import DEFAULT_HANDLER_MODEL, QuoteHandler
from .ingestion import parse_eml
from .models import (
    Classification,
    CustomerMatch,
    HandlerDraft,
    InboundEmail,
    Intent,
    Outcome,
    PricingAgreementMatch,
    QuoteResult,
    ResolvedLine,
    RunRecord,
    ToolTrace,
)
from .tools import QuoteTools


class ClassifierLike(Protocol):
    model: str

    def classify(self, email: InboundEmail) -> Classification: ...


class HandlerLike(Protocol):
    model: str

    def handle(self, email: InboundEmail) -> tuple[HandlerDraft, list[ToolTrace]]: ...


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class QuotePipeline:
    """Two model stages followed by a deterministic quote validator."""

    def __init__(
        self,
        classifier: ClassifierLike,
        handler: HandlerLike,
        tools: QuoteTools,
    ) -> None:
        self.classifier = classifier
        self.handler = handler
        self.tools = tools

    def run(self, eml_path: str | Path) -> RunRecord:
        return self.process(parse_eml(eml_path))

    def process(self, email: InboundEmail) -> RunRecord:
        """Process a parsed email and always produce an auditable terminal result."""

        run_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            classification = self.classifier.classify(email)
        except Exception as exc:
            classification = Classification(
                intent=Intent.UNKNOWN,
                confidence=0,
                rationale="Classifier failed before a business decision could be made.",
                evidence=[],
            )
            result = self._processing_error(email, "classifier_error", exc)
            return self._record(run_id, created_at, email, classification, None, [], result)

        if classification.intent == Intent.UNKNOWN:
            result = QuoteResult(
                outcome=Outcome.NEEDS_CLARIFICATION,
                reason_codes=["unknown_intent"],
                proposed_subject=f"Re: {email.subject}",
                proposed_body=(
                    "I could not determine whether this message is a new quote request. "
                    "Please confirm the products and quantities you would like quoted."
                ),
                issues=[classification.rationale],
            )
            return self._record(run_id, created_at, email, classification, None, [], result)

        if classification.intent != Intent.NEW_QUOTE_REQUEST:
            result = QuoteResult(
                outcome=Outcome.NOT_QUOTE,
                reason_codes=[f"intent_{classification.intent.value}"],
                proposed_subject=f"Re: {email.subject}",
                proposed_body=(
                    "This message was not identified as a new quote request and has been "
                    "left for manual review."
                ),
                issues=[classification.rationale],
            )
            return self._record(run_id, created_at, email, classification, None, [], result)

        draft, trace = None, []
        validation_trace: list[ToolTrace] = []
        try:
            draft, trace = self.handler.handle(email)
        except Exception as exc:
            result = self._processing_error(email, "handler_error", exc)
        else:
            try:
                result = self.resolve(email, draft, validation_trace=validation_trace)
            except Exception as exc:
                result = self._processing_error(email, "validation_error", exc)
        record = self._record(run_id, created_at, email, classification, draft, trace, result)
        record.validation_trace = validation_trace
        return record

    def _record(
        self,
        run_id: str,
        created_at: str,
        email: InboundEmail,
        classification: Classification,
        draft: HandlerDraft | None,
        trace: list[ToolTrace],
        result: QuoteResult,
    ) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            created_at=created_at,
            classifier_model=getattr(self.classifier, "model", DEFAULT_CLASSIFIER_MODEL),
            handler_model=(
                getattr(self.handler, "model", DEFAULT_HANDLER_MODEL) if draft is not None else None
            ),
            email=email,
            classification=classification,
            handler_draft=draft,
            tool_trace=trace,
            result=result,
        )

    @staticmethod
    def _processing_error(email: InboundEmail, code: str, error: Exception) -> QuoteResult:
        return QuoteResult(
            outcome=Outcome.PROCESSING_ERROR,
            reason_codes=[code],
            proposed_subject=f"Re: {email.subject}",
            proposed_body="The request could not be processed automatically and needs manual review.",
            issues=[f"{type(error).__name__}: automatic processing failed; manual review required."],
        )

    @staticmethod
    def _source_explicitly_names_id(email: InboundEmail, stock_item_id: int) -> bool:
        """Require an ID to originate in the email, never solely in model output."""

        text = "\n".join(
            [email.subject, email.body]
            + [attachment.extracted_text for attachment in email.attachments]
        )
        phrase = re.compile(
            rf"\b(?:stock\s*item(?:\s*id)?|sku)\s*[#:]?\s*{stock_item_id}\b",
            re.IGNORECASE,
        )
        if phrase.search(text):
            return True
        # Spreadsheet extraction preserves tabs and row boundaries. Accept a
        # numeric cell only when the same attachment declares an ID column.
        for attachment in email.attachments:
            normalized = re.sub(r"[^a-z0-9]", "", attachment.extracted_text.lower())
            if "stockitemid" in normalized and re.search(
                rf"(?m)(?:^|\t)\s*{stock_item_id}\s*(?:\t|$)",
                attachment.extracted_text,
            ):
                return True
        return False

    @staticmethod
    def _required_date_from_source(email: InboundEmail) -> str | None:
        """Resolve a requested date from source text, not the model's guessed year."""

        text = "\n".join(
            [email.subject, email.body]
            + [attachment.extracted_text for attachment in email.attachments]
        )
        discovered: set[date] = set()
        for match in re.finditer(r"\b(\d{4}-\d{2}-\d{2})(?:T[0-9:.+-]+)?\b", text):
            try:
                discovered.add(date.fromisoformat(match.group(1)))
            except ValueError:
                continue

        sent_date: date | None = None
        if email.sent_at:
            try:
                sent_date = parsedate_to_datetime(email.sent_at).date()
            except (TypeError, ValueError, OverflowError):
                pass
        month_pattern = (
            r"\b(January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b"
        )
        for match in re.finditer(month_pattern, text, re.IGNORECASE):
            month = MONTH_NUMBERS[match.group(1)[:3].lower()]
            explicit_year = int(match.group(3)) if match.group(3) else None
            if explicit_year is None and sent_date is None:
                continue
            year = explicit_year or sent_date.year
            try:
                candidate = date(year, month, int(match.group(2)))
                # An omitted year normally means the next occurrence of that
                # month/day relative to when the email was sent.
                if explicit_year is None and sent_date and candidate < sent_date:
                    candidate = date(year + 1, month, int(match.group(2)))
                discovered.add(candidate)
            except ValueError:
                continue
        return next(iter(discovered)).isoformat() if len(discovered) == 1 else None

    def resolve(
        self, email: InboundEmail, draft: HandlerDraft,
        *, validation_trace: list[ToolTrace] | None = None,
    ) -> QuoteResult:
        """Resolve products and calculate money without trusting model arithmetic."""

        def lookup(name: str, **arguments: Any) -> dict[str, Any]:
            # These are fresh business-rule lookups, independent of model research.
            try:
                result = getattr(self.tools, name)(**arguments)
            except Exception as exc:
                if validation_trace is not None:
                    validation_trace.append(ToolTrace(
                        name=name, arguments=arguments,
                        result={"error": f"{type(exc).__name__}: validation lookup failed"},
                    ))
                raise
            if validation_trace is not None:
                validation_trace.append(ToolTrace(name=name, arguments=arguments, result=result))
            return result

        customer = CustomerMatch.model_validate(lookup(
            "find_customer", sender_email=email.from_address, customer_hint=draft.customer_hint,
        ))
        reason_codes: list[str] = []
        issues: list[str] = []
        lines: list[ResolvedLine] = []
        pricing_agreements: list[PricingAgreementMatch] = []

        if customer.customer_id is None or customer.match_type in {
            "conflict",
            "ambiguous_sender",
            "ambiguous_hint",
        }:
            _append_unique(reason_codes, "customer_unresolved")
            issues.append(customer.evidence or "Customer could not be uniquely resolved")
        if not draft.lines:
            _append_unique(reason_codes, "no_line_items")
            issues.append("No quote line items were extracted")

        if email.ingestion_issues or any(a.extraction_error for a in email.attachments):
            _append_unique(reason_codes, "incomplete_source")
            issues.append("Email or attachment extraction is incomplete; review the original request")

        quantities_by_id: dict[int, Decimal] = {}
        stock_by_id: dict[int, int] = {}
        prices_by_id: dict[int, tuple[Decimal, Decimal]] = {}
        subtotal = Decimal("0")
        tax_total = Decimal("0")
        for request in draft.lines:
            resolved = ResolvedLine(
                requested_description=request.description,
                requested_quantity=request.quantity,
                requested_unit=request.unit,
            )
            search_id = (
                request.stock_item_id_hint
                if request.stock_item_id_hint
                and self._source_explicitly_names_id(email, request.stock_item_id_hint)
                else None
            )
            search = lookup("search_products", query=request.description, stock_item_id=search_id)
            candidates = search["candidates"]
            auto_candidates = [
                item for item in candidates if item["match_type"] in {"exact_id", "exact_name", "alias"}
            ]
            if len(auto_candidates) != 1:
                resolved.issues.append("product_unresolved")
                _append_unique(reason_codes, "product_unresolved")
                candidate_names = [item["stock_item_name"] for item in candidates[:3]]
                issues.append(
                    f"Could not uniquely resolve {request.description!r}"
                    + (f"; candidates: {', '.join(candidate_names)}" if candidate_names else "")
                )
                lines.append(resolved)
                continue

            product = auto_candidates[0]
            stock_item_id = int(product["stock_item_id"])
            resolved.stock_item_id = stock_item_id
            resolved.stock_item_name = product["stock_item_name"]
            resolved.match_type = product["match_type"]

            conversion = lookup(
                "normalize_quantity", stock_item_id=stock_item_id,
                quantity=request.quantity, from_unit=request.unit,
            )
            if "error" in conversion:
                resolved.issues.append("unit_conversion_missing")
                _append_unique(reason_codes, "unit_conversion_missing")
                issues.append(str(conversion["error"]))
                lines.append(resolved)
                continue
            quantity = Decimal(str(conversion["normalized_quantity"]))
            if (not quantity.is_finite() or quantity <= 0
                    or quantity != quantity.to_integral_value()):
                resolved.issues.append("invalid_quantity")
                _append_unique(reason_codes, "invalid_quantity")
                issues.append("Requested quantities must resolve to positive whole stock units")
                lines.append(resolved)
                continue
            resolved.normalized_quantity = float(quantity)
            resolved.normalized_unit = conversion["normalized_unit"]
            if float(conversion["multiplier"]) != 1:
                resolved.issues.append("uom_conversion")
                _append_unique(reason_codes, "uom_conversion")

            inventory = lookup("get_inventory", stock_item_id=stock_item_id)
            price = lookup("get_catalog_price", stock_item_id=stock_item_id)
            if "error" in inventory or "error" in price:
                resolved.issues.append("catalog_lookup_failed")
                _append_unique(reason_codes, "catalog_lookup_failed")
                issues.append(str(inventory.get("error") or price.get("error")))
                lines.append(resolved)
                continue

            unit_price = Decimal(str(price["unit_price"]))
            tax_rate = Decimal(str(price["tax_rate_percent"]))
            previous_price = prices_by_id.get(stock_item_id)
            if (
                not unit_price.is_finite() or unit_price < 0
                or not tax_rate.is_finite() or not 0 <= tax_rate <= 100
                or price.get("price_unit") != resolved.normalized_unit
                or inventory.get("stock_unit") != resolved.normalized_unit
                or price.get("stock_item_id") != stock_item_id
                or inventory.get("stock_item_id") != stock_item_id
                or (previous_price is not None and previous_price != (unit_price, tax_rate))
            ):
                resolved.issues.append("catalog_data_invalid")
                _append_unique(reason_codes, "catalog_data_invalid")
                issues.append("Catalog price, tax, unit, or item identity requires review")
                lines.append(resolved)
                continue
            prices_by_id[stock_item_id] = (unit_price, tax_rate)
            stock = Decimal(str(inventory["quantity_on_hand"]))
            if not stock.is_finite() or stock < 0 or stock != stock.to_integral_value():
                resolved.issues.append("inventory_data_invalid")
                _append_unique(reason_codes, "inventory_data_invalid")
                issues.append("Inventory must be a nonnegative whole stock-unit count")
                lines.append(resolved)
                continue
            resolved.quantity_on_hand = int(stock)
            resolved.catalog_unit_price = float(unit_price)
            resolved.tax_rate_percent = float(tax_rate)
            quantities_by_id[stock_item_id] = quantities_by_id.get(stock_item_id, Decimal(0)) + quantity
            stock_by_id[stock_item_id] = min(stock_by_id.get(stock_item_id, int(stock)), int(stock))

            line_subtotal = _money(quantity * unit_price)
            line_tax = _money(line_subtotal * tax_rate / Decimal("100"))
            resolved.line_subtotal = float(line_subtotal)
            resolved.tax_amount = float(line_tax)
            resolved.line_total = float(line_subtotal + line_tax)
            subtotal += line_subtotal
            tax_total += line_tax
            lines.append(resolved)

        # Repeated lines share inventory; they cannot each consume the same stock.
        for line in lines:
            item_id = line.stock_item_id
            if item_id in quantities_by_id and quantities_by_id[item_id] > stock_by_id[item_id]:
                line.issues.append("insufficient_stock")
                _append_unique(reason_codes, "insufficient_stock")
        for item_id, quantity in quantities_by_id.items():
            if quantity > stock_by_id[item_id]:
                issues.append(
                    f"Stock item {item_id}: combined request {quantity:g} exceeds "
                    f"{stock_by_id[item_id]} on hand"
                )

        if draft.pricing_agreement_hints:
            _append_unique(reason_codes, "pricing_agreement_review")
            as_of_date: str | None = None
            if email.sent_at:
                try:
                    as_of_date = parsedate_to_datetime(email.sent_at).date().isoformat()
                except (TypeError, ValueError, OverflowError):
                    pass
            if customer.customer_id is not None and as_of_date:
                agreement_lookup = lookup(
                    "find_pricing_agreements", customer_id=customer.customer_id,
                    as_of_date=as_of_date, query=" ".join(draft.pricing_agreement_hints),
                )
                candidates = agreement_lookup.get("candidates", [])
                if len(candidates) == 1:
                    agreement_data = dict(candidates[0])
                    agreement_data.pop("score", None)
                    pricing_agreements.append(PricingAgreementMatch.model_validate(agreement_data))
                    issues.append(
                        f"Pricing agreement requires review: {pricing_agreements[0].agreement_id}"
                    )
                else:
                    _append_unique(reason_codes, "pricing_agreement_unresolved")
                    issues.append(
                        "Pricing agreement was mentioned but could not be uniquely resolved"
                    )
            else:
                _append_unique(reason_codes, "pricing_agreement_unresolved")
                issues.append("Pricing agreement could not be resolved without customer and email date")

        required_date = self._required_date_from_source(email)
        if draft.required_date and required_date is None:
            _append_unique(reason_codes, "required_date_unverified")
            issues.append("A requested delivery date was mentioned but could not be verified uniquely")

        # One primary outcome keeps downstream routing simple. Reason codes retain
        # secondary facts, such as a unit conversion and a stock shortage together.
        if draft.pricing_agreement_hints or "catalog_data_invalid" in reason_codes:
            outcome = Outcome.PRICING_REVIEW
        elif any(
            code in reason_codes
            for code in (
                "customer_unresolved",
                "incomplete_source",
                "invalid_quantity",
                "inventory_data_invalid",
                "required_date_unverified",
                "no_line_items",
                "product_unresolved",
                "unit_conversion_missing",
                "catalog_lookup_failed",
            )
        ):
            outcome = Outcome.NEEDS_CLARIFICATION
        elif "insufficient_stock" in reason_codes:
            outcome = Outcome.INSUFFICIENT_STOCK
        else:
            outcome = Outcome.QUOTE_READY

        financials_complete = outcome == Outcome.QUOTE_READY
        total = subtotal + tax_total
        body = self._response_body(
            email,
            required_date,
            outcome,
            lines,
            issues,
            subtotal,
            tax_total,
            total,
        )
        return QuoteResult(
            outcome=outcome,
            reason_codes=reason_codes,
            customer=customer,
            pricing_agreements=pricing_agreements,
            lines=lines,
            subtotal=float(subtotal) if financials_complete else None,
            tax_total=float(tax_total) if financials_complete else None,
            total=float(total) if financials_complete else None,
            proposed_subject=f"Re: {email.subject}",
            proposed_body=body,
            issues=issues,
        )

    @staticmethod
    def _response_body(
        email: InboundEmail,
        required_date: str | None,
        outcome: Outcome,
        lines: list[ResolvedLine],
        issues: list[str],
        subtotal: Decimal,
        tax_total: Decimal,
        total: Decimal,
    ) -> str:
        greeting = f"Hello {email.sender.split()[0]}," if email.sender else "Hello,"
        if outcome == Outcome.QUOTE_READY:
            details = [
                f"- {line.normalized_quantity:g} {line.normalized_unit} of "
                f"{line.stock_item_name} at ${line.catalog_unit_price:,.2f} each"
                for line in lines
            ]
            delivery = (
                f" We noted your requested date ({required_date}); it remains subject to confirmation."
                if required_date
                else ""
            )
            return "\n".join(
                [
                    greeting,
                    "",
                    "Here is our draft catalog-price quote:",
                    *details,
                    "",
                    f"Subtotal: ${subtotal:,.2f}",
                    f"Tax: ${tax_total:,.2f}",
                    f"Total: ${total:,.2f}",
                    "Freight: TBD.",
                    delivery.strip(),
                    "",
                    "Please let us know if you would like to proceed.",
                ]
            ).replace("\n\n\n", "\n\n")
        if outcome == Outcome.INSUFFICIENT_STOCK:
            return (
                f"{greeting}\n\nWe cannot produce the requested quote because the full quantity "
                f"is not currently available. {'; '.join(issues)}"
            )
        if outcome == Outcome.PRICING_REVIEW:
            return (
                f"{greeting}\n\nPricing needs manual review before a quote can be issued."
            )
        return (
            f"{greeting}\n\nWe need a little more information before preparing the quote: "
            + "; ".join(issues)
        )
