# Wide World Importers for SQLite

This project converts Microsoft's Wide World Importers Standard OLTP and warehouse BACPACs directly into two portable SQLite databases. It uses only the Python standard library: SQL Server, Azure, Docker, .NET, and external database drivers are not required.

The generated databases contain all 77 base tables, including archive and empty staging tables:

- `data/sqlite/wide_world_importers.sqlite3` — 48 tables and 4,713,833 rows
- `data/sqlite/wide_world_importers_dw.sqlite3` — 29 tables and 923,643 rows

Generated BACPACs, databases, and the build manifest are intentionally gitignored.

## Build

From this repository's root:

```sh
python3 -m wwi_sqlite build --output-dir data/sqlite
```

The command downloads missing BACPACs to `data/source`, verifies their pinned SHA-256 hashes, streams each native BCP part into a temporary SQLite file, validates it, and atomically publishes it. The versioned checksum list is in `sources.sha256`; the same pinned values are enforced by the converter.

Existing outputs are never replaced unless requested:

```sh
python3 -m wwi_sqlite build --output-dir data/sqlite --force
```

For a network-free rebuild, retain the two verified files in `data/source` and run:

```sh
python3 -m wwi_sqlite build --output-dir data/sqlite --offline --force
```

Repeat the full row-count, integrity, foreign-key, and representative-type checks at any time:

```sh
python3 -m wwi_sqlite verify --output-dir data/sqlite
```

## Query from Python

```python
import sqlite3

connection = sqlite3.connect("data/sqlite/wide_world_importers.sqlite3")
rows = connection.execute(
    """
    SELECT customer_name, phone_number
    FROM sales__customers
    ORDER BY customer_name
    LIMIT 10
    """
).fetchall()
print(rows)
```

Identifiers are normalized to snake case. SQL Server schemas are joined to table names with a double underscore, such as `Sales.Orders` → `sales__orders`. `_meta_identifier_map` records every original table name, column name, SQL type, and normalized SQLite identifier.

## Query with sqlite3 or a browser

SQLite is file-based; there is no server process to start. Open either `.sqlite3` file directly in [DB Browser for SQLite](https://sqlitebrowser.org/) or another standard SQLite browser.

```sh
sqlite3 data/sqlite/wide_world_importers.sqlite3
```

Then, for example:

```sql
.tables
.headers on
.mode column
SELECT order_id, order_date, is_undersupply_backordered
FROM sales__orders
LIMIT 5;
```

## Portability choices

- Integers and bits use `INTEGER`.
- Dates and `datetime2` values use ISO-8601 `TEXT`; timestamps retain seven fractional digits.
- Decimals use canonical fixed-scale `TEXT`, avoiding binary floating-point loss.
- Unicode uses `TEXT`; binary values use `BLOB`.
- Geography retains the original SQL Server serialization as `BLOB` and adds adjacent `__wkt` and `__srid` columns.
- Eight OLTP computed columns are materialized, including JSON-derived and finalized-status values.
- Primary keys, unique constraints, foreign keys, and portable B-tree indexes are recreated. SQL Server-only executable and operational objects are deliberately omitted.

`data/sqlite/build_manifest.json` records source URLs and hashes, output sizes, row and schema counts, and runtime versions for each build.

## Tests

```sh
python3 -m unittest discover -s tests -v
```


The quoting demo uses a tiny independent synthetic database and does not require these downloads.
