#!/usr/bin/env python3
"""Verify the generated WWI portfolio document fixture pack."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile


EXPECTED_TYPES = {
    ".csv": 9,
    ".docx": 1,
    ".eml": 39,
    ".json": 5,
    ".md": 1,
    ".pdf": 20,
    ".xlsx": 3,
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def verify(root: Path, oltp_path: Path, dw_path: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store"]
    counts = Counter(path.suffix.lower() for path in files)
    if counts != Counter({**EXPECTED_TYPES, ".json": EXPECTED_TYPES[".json"] + 1}):
        # manifest.json is the sixth JSON file on disk but is not an artifact.
        raise AssertionError(f"unexpected artifact inventory: {dict(sorted(counts.items()))}")

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        path.relative_to(root).as_posix()
        for path in files
        if path.name != "manifest.json"
    }
    recorded = {item["path"] for item in manifest["artifacts"]}
    if manifest["artifact_count"] != 78 or actual != recorded:
        raise AssertionError("manifest paths do not match the generated fixture pack")
    for item in manifest["artifacts"]:
        path = root / item["path"]
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise AssertionError(f"manifest mismatch: {path}")

    attachment_count = 0
    for path in root.rglob("*.eml"):
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        required = ["From", "To", "Subject", "Date", "Message-ID"]
        if any(not message[name] for name in required):
            raise AssertionError(f"missing email header: {path}")
        body = message.get_body(preferencelist=("plain",))
        if body is None or not body.get_content().strip():
            raise AssertionError(f"missing email body: {path}")
        for part in message.iter_attachments():
            attachment_count += 1
            candidates = list(root.rglob(part.get_filename()))
            payload = part.get_payload(decode=True)
            if not candidates or not any(item.read_bytes() == payload for item in candidates):
                raise AssertionError(f"detached or altered attachment: {path}")
    if attachment_count != 12:
        raise AssertionError(f"expected 12 embedded attachments, got {attachment_count}")

    for path in [*root.rglob("*.xlsx"), *root.rglob("*.docx")]:
        if not zipfile.is_zipfile(path):
            raise AssertionError(f"invalid Office file: {path}")
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise AssertionError(f"corrupt Office file: {path}")
    for path in root.rglob("*.pdf"):
        content = path.read_bytes()
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
            raise AssertionError(f"invalid PDF envelope: {path}")

    clean = root / "03_invoice_matching/invoices/ap_001_clean_FAB-160601.pdf"
    duplicate = root / "03_invoice_matching/invoices/ap_002_duplicate_renamed.pdf"
    if clean.read_bytes() != duplicate.read_bytes():
        raise AssertionError("duplicate-invoice fixtures must be byte-identical")

    pricing = csv_rows(root / "01_margin_operations/inputs/applied_pricing_decisions.csv")
    receipts = csv_rows(root / "03_invoice_matching/receipts/goods_received.csv")
    payments = csv_rows(root / "05_receivables/payments/bank_receipts.csv")
    if len(pricing) != 9:
        raise AssertionError("expected nine applied-pricing records")
    if not any(row["purchase_order_line_id"] == "8359" and row["received_outers"] == "60" for row in receipts):
        raise AssertionError("partial receipt fixture is missing")
    if len(payments) != 2 or any(not row["invoice_id"] for row in payments):
        raise AssertionError("payment allocation fixtures are incomplete")

    with sqlite3.connect(f"file:{oltp_path.resolve()}?mode=ro", uri=True) as oltp:
        for purchase_order_id in (2065, 2068, 2069, 2070, 2071, 2073, 2074):
            found = oltp.execute(
                "SELECT 1 FROM purchasing__purchase_orders WHERE purchase_order_id=?",
                (purchase_order_id,),
            ).fetchone()
            if found is None:
                raise AssertionError(f"missing source purchase order {purchase_order_id}")
        for invoice_id in range(70427, 70439):
            found = oltp.execute(
                "SELECT 1 FROM sales__invoices WHERE invoice_id=?", (invoice_id,)
            ).fetchone()
            if found is None:
                raise AssertionError(f"missing source invoice {invoice_id}")

    with sqlite3.connect(f"file:{dw_path.resolve()}?mode=ro", uri=True) as warehouse:
        rows = warehouse.execute(
            """
            SELECT invoice_date_key, ROUND(SUM(CAST(total_excluding_tax AS REAL)), 2),
                   ROUND(SUM(CAST(profit AS REAL)), 2)
            FROM fact__sale
            WHERE invoice_date_key IN ('2016-04-21', '2016-04-22')
            GROUP BY invoice_date_key ORDER BY invoice_date_key
            """
        ).fetchall()
        if rows != [("2016-04-21", 200926.8, 98315.15), ("2016-04-22", 206672.9, 97971.6)]:
            raise AssertionError("margin comparison no longer matches the WWI warehouse")

    print("Portfolio fixture verification passed")
    print(f"  {len(files)} files on disk; 78 artifacts plus manifest")
    print("  39 emails with 12 embedded attachments")
    print("  20 PDFs, 3 XLSX workbooks, 1 DOCX template")
    print("  WWI purchase-order, invoice, and margin anchors confirmed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("data/portfolio_artifacts"))
    parser.add_argument("--oltp", type=Path, default=Path("data/sqlite/wide_world_importers.sqlite3"))
    parser.add_argument("--dw", type=Path, default=Path("data/sqlite/wide_world_importers_dw.sqlite3"))
    args = parser.parse_args()
    verify(args.output_dir, args.oltp, args.dw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
