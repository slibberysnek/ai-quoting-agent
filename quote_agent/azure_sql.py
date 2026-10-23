"""Azure SQL implementation of the quote agent's narrow read-only tools.

The public methods intentionally match :class:`quote_agent.tools.QuoteTools`.
That lets the tested agent pipeline use SQLite locally and the existing Azure
SQL data warehouse when hosted, without branching inside the business logic.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .models import CustomerMatch
from .tools import DEFAULT_REFERENCE_DIR, QuoteTools


class AzureSqlError(RuntimeError):
    """Raised when the Azure SQL configuration or a fixed query fails."""


class AzureSqlQuoteTools(QuoteTools):
    """Read WWI dimensions and stock facts through fixed parameterized SQL."""

    def __init__(
        self,
        connection_string: str,
        reference_dir: str | Path = DEFAULT_REFERENCE_DIR,
        *,
        connect: Callable[[str], Any] | None = None,
        catalog_cache_seconds: float = 60.0,
    ) -> None:
        if not connection_string.strip():
            raise ValueError("Azure SQL connection string is required")

        # Do not assign a SQLite path or call the parent constructor: this
        # backend shares its reference-data rules, not its connection method.
        self.connection_string = connection_string.strip()
        self._connector = connect
        self._catalog_cache_seconds = max(0.0, catalog_cache_seconds)
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._catalog_cache_expires_at = 0.0
        self._catalog_lock = threading.Lock()
        self._load_reference_data(reference_dir)

    @classmethod
    def from_environment(
        cls,
        reference_dir: str | Path = DEFAULT_REFERENCE_DIR,
    ) -> "AzureSqlQuoteTools":
        """Create the backend from one App Service configuration setting."""

        connection_string = os.environ.get("AZURE_SQL_CONNECTION_STRING", "").strip()
        if not connection_string:
            raise AzureSqlError("missing environment setting: AZURE_SQL_CONNECTION_STRING")
        return cls(connection_string, reference_dir)

    def _connect_azure_sql(self) -> Any:
        """Import Microsoft's driver lazily so SQLite-only runs stay lightweight."""

        if self._connector is not None:
            return self._connector(self.connection_string)
        try:
            import mssql_python
        except ImportError as exc:  # pragma: no cover - exercised after deployment install.
            raise AzureSqlError(
                "mssql-python is not installed; install the project cloud extra: pip install -e '.[cloud]'"
            ) from exc
        try:
            return mssql_python.connect(self.connection_string)
        except Exception as exc:  # The driver has several backend-specific error subclasses.
            raise AzureSqlError(f"could not connect to Azure SQL: {type(exc).__name__}") from exc

    def _fetch_rows(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """Execute one fixed query and return rows keyed by their SQL aliases."""

        connection = self._connect_azure_sql()
        cursor = None
        try:
            cursor = connection.cursor()
            if parameters:
                cursor.execute(sql, tuple(parameters))
            else:
                cursor.execute(sql)
            columns = [description[0] for description in (cursor.description or [])]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        except AzureSqlError:
            raise
        except Exception as exc:
            # The connection string can contain the client secret, so errors
            # deliberately describe only the operation and driver response.
            raise AzureSqlError(f"Azure SQL query failed: {type(exc).__name__}") from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass

    def _catalog(self) -> list[dict[str, Any]]:
        """Return current products and holdings from the WWI star schema."""

        # One quote may ask for several products. A short cache prevents each
        # tool call from downloading the same small catalog while keeping stock
        # values reasonably fresh for a human-reviewed proposal.
        with self._catalog_lock:
            now = time.monotonic()
            if self._catalog_cache is not None and now < self._catalog_cache_expires_at:
                return self._catalog_cache

            rows = self._fetch_rows(
                """
                SELECT
                    si.[WWI Stock Item ID] AS stock_item_id,
                    si.[Stock Item] AS stock_item_name,
                    si.[Quantity Per Outer] AS quantity_per_outer,
                    si.[Unit Price] AS unit_price,
                    si.[Tax Rate] AS tax_rate,
                    si.[Selling Package] AS unit_name,
                    sh.[Quantity On Hand] AS quantity_on_hand
                FROM [Dimension].[Stock Item] AS si
                INNER JOIN [Fact].[Stock Holding] AS sh
                    ON sh.[Stock Item Key] = si.[Stock Item Key]
                WHERE si.[Valid To] > SYSUTCDATETIME()
                ORDER BY si.[WWI Stock Item ID]
                """
            )
            catalog = [
                {
                    **row,
                    "stock_item_id": int(row["stock_item_id"]),
                    "quantity_per_outer": int(row["quantity_per_outer"]),
                    "quantity_on_hand": int(row["quantity_on_hand"]),
                }
                for row in rows
            ]
            self._catalog_cache = catalog
            self._catalog_cache_expires_at = now + self._catalog_cache_seconds
            return catalog

    def find_customer(
        self,
        sender_email: str,
        customer_hint: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a customer name when the DW lacks contact email addresses.

        The OLTP sample stores contact emails, but the DW's Customer dimension
        does not. We therefore use only an explicit name extracted from the
        email and leave ambiguous matches for the Teams reviewer.
        """

        del sender_email  # Document that this input cannot be matched in this schema.
        hint = (customer_hint or "").strip()
        if not hint:
            return CustomerMatch(
                match_type="not_found",
                evidence="The DW has no contact-email field and no customer name was supplied",
            ).model_dump()

        rows = self._fetch_rows(
            """
            SELECT TOP (10)
                [WWI Customer ID] AS customer_id,
                [Customer] AS customer_name,
                [Primary Contact] AS primary_contact
            FROM [Dimension].[Customer]
            WHERE [Valid To] > SYSUTCDATETIME()
              AND LOWER([Customer]) LIKE ?
            ORDER BY
                CASE WHEN LOWER([Customer]) = ? THEN 0 ELSE 1 END,
                [WWI Customer ID]
            """,
            (f"%{hint.lower()}%", hint.lower()),
        )
        if len(rows) == 1:
            row = rows[0]
            return CustomerMatch(
                customer_id=int(row["customer_id"]),
                customer_name=str(row["customer_name"]),
                match_type="name_hint",
                evidence=(
                    f"Matched customer hint {hint!r}; DW primary contact is "
                    f"{row['primary_contact']}"
                ),
            ).model_dump()
        return CustomerMatch(
            match_type="not_found" if not rows else "ambiguous_hint",
            evidence=(
                "No customer matched the extracted name"
                if not rows
                else f"Customer hint {hint!r} matched {len(rows)} current DW customers"
            ),
        ).model_dump()
