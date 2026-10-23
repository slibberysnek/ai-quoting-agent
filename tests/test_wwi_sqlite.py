from __future__ import annotations

from datetime import date
import io
import json
import struct
import unittest

from wwi_sqlite.bcp import iter_rows
from wwi_sqlite.converter import _computed_value
from wwi_sqlite.geography import decode_geography
from wwi_sqlite.model import Column, snake_case, sqlite_table_name


def column(sql_type: str, **kwargs) -> Column:
    return Column(
        source_full_name=f"[S].[T].[C]",
        source_name="C",
        sqlite_name="c",
        sql_type=sql_type,
        nullable=kwargs.pop("nullable", False),
        ordinal=0,
        **kwargs,
    )


class IdentifierTests(unittest.TestCase):
    def test_snake_case(self):
        self.assertEqual(snake_case("StockItemTransactions"), "stock_item_transactions")
        self.assertEqual(sqlite_table_name("Dimension", "Payment Method"), "dimension__payment_method")


class BCPTests(unittest.TestCase):
    def test_integer_date_datetime_decimal_and_text(self):
        decimal_payload = bytes((10, 2, 1)) + (12345).to_bytes(16, "little")
        text = "café".encode("utf-16le")
        day_count = (date(2020, 2, 29) - date(1, 1, 1)).days
        datetime_time = (12 * 3600 + 34 * 60 + 56) * 10_000_000 + 1_234_567
        payload = b"".join(
            (
                struct.pack("<i", -42),
                day_count.to_bytes(3, "little"),
                datetime_time.to_bytes(5, "little") + day_count.to_bytes(3, "little"),
                bytes((19,)) + decimal_payload,
                struct.pack("<H", len(text)) + text,
            )
        )
        columns = [
            column("int"),
            column("date"),
            column("datetime2", scale=7),
            column("decimal", precision=10, scale=2),
            column("nvarchar", length=20),
        ]
        rows = list(iter_rows(io.BytesIO(payload), columns))
        self.assertEqual(
            rows,
            [(-42, "2020-02-29", "2020-02-29 12:34:56.1234567", "123.45", "café")],
        )

    def test_nullable_and_chunked_max(self):
        text = "hello".encode("utf-16le")
        payload = b"\xff" + struct.pack("<q", -2) + struct.pack("<I", len(text)) + text + b"\0\0\0\0"
        rows = list(
            iter_rows(
                io.BytesIO(payload),
                [column("int", nullable=True), column("nvarchar", nullable=True, is_max=True)],
            )
        )
        self.assertEqual(rows, [(None, "hello")])


class GeographyTests(unittest.TestCase):
    def test_single_point(self):
        raw = struct.pack("<iBBdd", 4326, 1, 0x0C, 47.61, -122.33)
        wkt, srid = decode_geography(raw)
        self.assertEqual(srid, 4326)
        self.assertEqual(wkt, "POINT (-122.33 47.61)")


class ComputedColumnTests(unittest.TestCase):
    def test_search_name(self):
        computed = column("computed")
        computed.computed = True
        computed.source_full_name = "[Application].[People].[SearchName]"
        self.assertEqual(
            _computed_value(computed, {"PreferredName": "Ada", "FullName": "Ada Lovelace"}),
            "Ada Ada Lovelace",
        )

    def test_json_query(self):
        computed = column("computed")
        computed.computed = True
        computed.source_full_name = "[Warehouse].[StockItems].[Tags]"
        self.assertEqual(
            _computed_value(computed, {"CustomFields": json.dumps({"Tags": ["red", "sale"]})}),
            '["red","sale"]',
        )

    def test_computed_delivery_timestamp(self):
        computed = column("computed")
        computed.computed = True
        computed.source_full_name = "[Sales].[Invoices].[ConfirmedDeliveryTime]"
        self.assertEqual(
            _computed_value(
                computed,
                {"ReturnedDeliveryData": '{"DeliveredWhen":"2013-01-02T07:05:00"}'},
            ),
            "2013-01-02 07:05:00.0000000",
        )


if __name__ == "__main__":
    unittest.main()
