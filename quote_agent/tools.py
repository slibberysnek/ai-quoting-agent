"""Narrow, read-only tools exposed to the quote-handling agent.

The model can search known entities and retrieve facts, but it cannot issue SQL.
Every query is fixed and parameterized, which keeps the database boundary easy
to understand and test before it is replaced with Azure SQL.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import CustomerMatch


DEFAULT_REFERENCE_DIR = Path(__file__).with_name("reference_data")


def _normalize(value: str) -> str:
    """Create a forgiving comparison form for catalog and email language."""

    words = re.findall(r"[a-z0-9]+", value.lower())
    singular = [word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words]
    return " ".join(singular)


def _tokens(value: str) -> set[str]:
    return set(_normalize(value).split())


class QuoteTools:
    """Read-only WWI catalog, customer, inventory, and unit-conversion tools."""

    price_source = "WWI catalog"

    def __init__(
        self,
        database_path: str | Path,
        reference_dir: str | Path = DEFAULT_REFERENCE_DIR,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        if not self.database_path.exists():
            raise FileNotFoundError(self.database_path)
        self._load_reference_data(reference_dir)

    def _load_reference_data(self, reference_dir: str | Path) -> None:
        """Load small business rules shared by SQLite and Azure SQL backends."""

        self.reference_dir = Path(reference_dir).resolve()
        if not self.reference_dir.exists():
            raise FileNotFoundError(self.reference_dir)
        self.aliases = self._read_csv("product_aliases.csv")
        self.conversions = self._read_csv("uom_conversions.csv")
        self.agreements = self._read_csv("customer_pricing_agreements.csv")

    def _read_csv(self, filename: str) -> list[dict[str, str]]:
        with (self.reference_dir / filename).open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def _connect(self) -> sqlite3.Connection:
        # URI read-only mode gives us a database-level guard in addition to the
        # absence of write methods in this class.
        connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def find_customer(self, sender_email: str, customer_hint: str | None = None) -> dict[str, Any]:
        """Find the current customer by contact email, optionally checking a hint."""

        email = sender_email.strip().lower()
        # sqlite3's context manager controls transactions but does not close
        # the connection, so use closing() for deterministic cleanup.
        with closing(self._connect()) as connection:
            sender_rows = connection.execute(
                """
                SELECT c.customer_id, c.customer_name, p.full_name, p.email_address
                FROM sales__customers AS c
                JOIN application__people AS p
                  ON p.person_id IN (c.primary_contact_person_id, c.alternate_contact_person_id)
                WHERE lower(p.email_address) = ?
                  AND c.valid_to LIKE '9999-%'
                  AND p.valid_to LIKE '9999-%'
                ORDER BY c.customer_id
                """,
                (email,),
            ).fetchall()
            hint_rows = []
            if customer_hint:
                hint_rows = connection.execute(
                    """
                    SELECT customer_id, customer_name
                    FROM sales__customers
                    WHERE lower(customer_name) LIKE ? AND valid_to LIKE '9999-%'
                    ORDER BY customer_id LIMIT 10
                    """,
                    (f"%{customer_hint.strip().lower()}%",),
                ).fetchall()

        if len(sender_rows) == 1:
            row = sender_rows[0]
            hint_ids = {candidate["customer_id"] for candidate in hint_rows}
            conflict = bool(hint_ids and row["customer_id"] not in hint_ids)
            return CustomerMatch(
                customer_id=row["customer_id"],
                customer_name=row["customer_name"],
                match_type="conflict" if conflict else "sender_email",
                evidence=f"Contact email {row['email_address']} belongs to {row['full_name']}",
            ).model_dump()
        if len(sender_rows) > 1:
            return CustomerMatch(
                match_type="ambiguous_sender",
                evidence=f"Sender email matched {len(sender_rows)} customers",
            ).model_dump()
        if len(hint_rows) == 1:
            return CustomerMatch(
                customer_id=hint_rows[0]["customer_id"],
                customer_name=hint_rows[0]["customer_name"],
                match_type="name_hint",
                evidence=f"Matched model-extracted customer hint {customer_hint!r}",
            ).model_dump()
        return CustomerMatch(
            match_type="not_found" if not hint_rows else "ambiguous_hint",
            evidence="No unique customer could be established",
        ).model_dump()

    def _catalog(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT si.stock_item_id, si.stock_item_name, si.quantity_per_outer,
                       si.unit_price, si.tax_rate, pt.package_type_name AS unit_name,
                       h.quantity_on_hand
                FROM warehouse__stock_items AS si
                JOIN warehouse__package_types AS pt
                  ON pt.package_type_id = si.unit_package_id
                JOIN warehouse__stock_item_holdings AS h
                  ON h.stock_item_id = si.stock_item_id
                WHERE si.valid_to LIKE '9999-%'
                ORDER BY si.stock_item_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def search_products(
        self,
        query: str,
        stock_item_id: int | None = None,
        *,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return ranked product candidates and explain how each one matched."""

        catalog = self._catalog()
        if stock_item_id is not None:
            exact = [item for item in catalog if item["stock_item_id"] == stock_item_id]
            return {
                "query": query,
                "candidates": [dict(item, match_type="exact_id", score=1.0) for item in exact],
            }

        normalized_query = _normalize(query)
        query_tokens = _tokens(query)
        alias_by_id: dict[int, list[str]] = {}
        for alias in self.aliases:
            alias_by_id.setdefault(int(alias["stock_item_id"]), []).append(alias["alias"])

        candidates: list[dict[str, Any]] = []
        for item in catalog:
            item_name = item["stock_item_name"]
            normalized_name = _normalize(item_name)
            name_tokens = _tokens(item_name)
            overlap = len(query_tokens & name_tokens) / max(1, len(query_tokens | name_tokens))
            score = max(SequenceMatcher(None, normalized_query, normalized_name).ratio(), overlap)
            match_type = "search"
            if normalized_query == normalized_name:
                score, match_type = 1.0, "exact_name"
            for alias in alias_by_id.get(item["stock_item_id"], []):
                alias_tokens = _tokens(alias)
                alias_score = SequenceMatcher(None, normalized_query, _normalize(alias)).ratio()
                # Requiring every alias token avoids turning a missing carton
                # dimension into a falsely confident alias match.
                if match_type != "exact_name" and alias_tokens and alias_tokens <= query_tokens:
                    alias_score, match_type = 0.99, "alias"
                if match_type != "exact_name" and alias_score > score:
                    score = alias_score
            if score >= 0.25:
                candidates.append(dict(item, match_type=match_type, score=round(score, 4)))
        candidates.sort(key=lambda item: (-item["score"], item["stock_item_id"]))
        return {"query": query, "candidates": candidates[:limit]}

    def normalize_quantity(
        self,
        stock_item_id: int,
        quantity: float,
        from_unit: str,
    ) -> dict[str, Any]:
        """Convert a requested package quantity into the catalog's stock unit."""

        product = self.search_products("", stock_item_id)["candidates"]
        if not product:
            return {"error": f"unknown stock_item_id {stock_item_id}"}
        unit_name = product[0]["unit_name"]
        if _normalize(from_unit) == _normalize(unit_name):
            return {
                "stock_item_id": stock_item_id,
                "input_quantity": quantity,
                "input_unit": from_unit,
                "normalized_quantity": quantity,
                "normalized_unit": unit_name,
                "multiplier": 1.0,
            }
        for conversion in self.conversions:
            if (
                int(conversion["stock_item_id"]) == stock_item_id
                and _normalize(conversion["from_unit"]) == _normalize(from_unit)
                and _normalize(conversion["to_unit"]) == _normalize(unit_name)
            ):
                multiplier = float(conversion["multiplier"])
                return {
                    "stock_item_id": stock_item_id,
                    "input_quantity": quantity,
                    "input_unit": from_unit,
                    "normalized_quantity": quantity * multiplier,
                    "normalized_unit": unit_name,
                    "multiplier": multiplier,
                }
        return {
            "error": (
                f"no approved conversion from {from_unit!r} to {unit_name!r} "
                f"for stock item {stock_item_id}"
            )
        }

    def get_inventory(self, stock_item_id: int) -> dict[str, Any]:
        candidates = self.search_products("", stock_item_id)["candidates"]
        if not candidates:
            return {"error": f"unknown stock_item_id {stock_item_id}"}
        item = candidates[0]
        return {
            "stock_item_id": stock_item_id,
            "stock_item_name": item["stock_item_name"],
            "quantity_on_hand": item["quantity_on_hand"],
            "stock_unit": item["unit_name"],
        }

    def get_catalog_price(self, stock_item_id: int) -> dict[str, Any]:
        candidates = self.search_products("", stock_item_id)["candidates"]
        if not candidates:
            return {"error": f"unknown stock_item_id {stock_item_id}"}
        item = candidates[0]
        return {
            "stock_item_id": stock_item_id,
            "stock_item_name": item["stock_item_name"],
            "unit_price": float(item["unit_price"]),
            "tax_rate_percent": float(item["tax_rate"]),
            "price_unit": item["unit_name"],
            "price_source": self.price_source,
        }

    def find_pricing_agreements(
        self,
        customer_id: int,
        as_of_date: str,
        query: str,
    ) -> dict[str, Any]:
        """Find active fixture agreements without applying their discounts."""

        # ISO dates compare lexicographically, so no database or date-parser
        # dependency is needed for this intentionally small reference file.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of_date):
            return {"error": f"invalid as_of_date {as_of_date!r}"}
        query_tokens = _tokens(query)
        candidates: list[dict[str, Any]] = []
        for agreement in self.agreements:
            if agreement["customer_id"] and int(agreement["customer_id"]) != customer_id:
                continue
            if not (agreement["start_date"] <= as_of_date <= agreement["end_date"]):
                continue
            agreement_tokens = _tokens(agreement["agreement_id"])
            informative_tokens = agreement_tokens - {"tt", as_of_date[:4]}
            score = len(query_tokens & informative_tokens)
            candidates.append(
                {
                    "agreement_id": agreement["agreement_id"],
                    "customer_id": customer_id,
                    "start_date": agreement["start_date"],
                    "end_date": agreement["end_date"],
                    "discount_percentage": float(agreement["discount_percentage"]),
                    "requires_review": agreement["requires_review"].lower() == "true",
                    "match_type": "active_reference",
                    "score": score,
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["agreement_id"]))
        if candidates and candidates[0]["score"] > 0:
            best_score = candidates[0]["score"]
            candidates = [item for item in candidates if item["score"] == best_score]
        return {"query": query, "as_of_date": as_of_date, "candidates": candidates}

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        """OpenAI-compatible function definitions sent through OpenRouter."""

        return [
            _tool(
                "find_customer",
                "Find a customer from the sender address and an optional name hint.",
                {
                    "sender_email": {"type": "string"},
                    "customer_hint": {"type": ["string", "null"]},
                },
                ["sender_email", "customer_hint"],
            ),
            _tool(
                "search_products",
                "Search the product catalog or retrieve an explicit stock item ID.",
                {
                    "query": {"type": "string"},
                    "stock_item_id": {"type": ["integer", "null"]},
                },
                ["query", "stock_item_id"],
            ),
            _tool(
                "normalize_quantity",
                "Convert a quantity in the requested unit into the stock unit.",
                {
                    "stock_item_id": {"type": "integer"},
                    "quantity": {"type": "number"},
                    "from_unit": {"type": "string"},
                },
                ["stock_item_id", "quantity", "from_unit"],
            ),
            _tool(
                "get_inventory",
                "Get current quantity on hand for one stock item.",
                {"stock_item_id": {"type": "integer"}},
                ["stock_item_id"],
            ),
            _tool(
                "get_catalog_price",
                "Get catalog unit price and tax rate for one stock item.",
                {"stock_item_id": {"type": "integer"}},
                ["stock_item_id"],
            ),
            _tool(
                "find_pricing_agreements",
                "Find active pricing agreements for a customer and email date; do not apply them.",
                {
                    "customer_id": {"type": "integer"},
                    "as_of_date": {"type": "string"},
                    "query": {"type": "string"},
                },
                ["customer_id", "as_of_date", "query"],
            ),
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch only explicitly registered read methods."""

        methods = {
            "find_customer": self.find_customer,
            "search_products": self.search_products,
            "normalize_quantity": self.normalize_quantity,
            "get_inventory": self.get_inventory,
            "get_catalog_price": self.get_catalog_price,
            "find_pricing_agreements": self.find_pricing_agreements,
        }
        method = methods.get(name)
        if method is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return method(**arguments)
        except (TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
            return {"error": f"{type(exc).__name__}: read-only tool failed"}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def tool_result_json(result: dict[str, Any]) -> str:
    """Serialize tool output consistently for the agent conversation."""

    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
