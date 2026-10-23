"""Build and verify the Wide World Importers SQLite databases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET

from . import __version__
from .bcp import BCPFormatError, iter_rows
from .geography import decode_geography
from .model import (
    DAC_NS,
    Column,
    DatabaseModel,
    Table,
    parse_model,
    quote_identifier,
    snake_case,
)


RELEASE_BASE = (
    "https://github.com/microsoft/sql-server-samples/releases/download/"
    "wide-world-importers-v1.0"
)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    key: str
    bacpac_name: str
    output_name: str
    sha256: str
    expected_tables: int
    expected_rows: int

    @property
    def url(self) -> str:
        return f"{RELEASE_BASE}/{self.bacpac_name}"


SOURCES = (
    SourceSpec(
        key="oltp",
        bacpac_name="WideWorldImporters-Standard.bacpac",
        output_name="wide_world_importers.sqlite3",
        sha256="7e43c8ef5919ceebb8e9558e8ea8af755ca3c2745561e934951a2bbf942831ca",
        expected_tables=48,
        expected_rows=4_713_833,
    ),
    SourceSpec(
        key="dw",
        bacpac_name="WideWorldImportersDW-Standard.bacpac",
        output_name="wide_world_importers_dw.sqlite3",
        sha256="96e9b87dfe3665aefde12a2c4decb835982803470d7f224b988d8f512db2a6c5",
        expected_tables=29,
        expected_rows=923_643,
    ),
)
SOURCE_BY_KEY = {source.key: source for source in SOURCES}


class ConversionError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_source(spec: SourceSpec, source_dir: Path, offline: bool) -> Path:
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / spec.bacpac_name
    if target.exists():
        actual = sha256_file(target)
        if actual != spec.sha256:
            raise ConversionError(
                f"Checksum mismatch for {target}: expected {spec.sha256}, received {actual}"
            )
        return target
    if offline:
        raise ConversionError(f"Offline mode requires the previously downloaded file {target}")

    temporary = source_dir / f".{spec.bacpac_name}.{uuid.uuid4().hex}.download"
    request = urllib.request.Request(spec.url, headers={"User-Agent": f"wwi-sqlite/{__version__}"})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = digest.hexdigest()
        if actual != spec.sha256:
            raise ConversionError(
                f"Checksum mismatch for downloaded {spec.bacpac_name}: "
                f"expected {spec.sha256}, received {actual}"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _origin_metadata(xml_bytes: bytes) -> tuple[str, int, dict[str, int]]:
    root = ET.fromstring(xml_bytes)
    namespace = {"d": DAC_NS}
    versions = {
        node.get("StreamName"): (node.text or "")
        for node in root.findall(".//d:StreamVersions/d:Version", namespace)
    }
    data_version = versions.get("Data", "")
    total_node = root.find(".//d:TableRowCountTotalTag", namespace)
    if total_node is None or total_node.text is None:
        raise ConversionError("Origin.xml has no embedded table-row total")
    counts: dict[str, int] = {}
    object_counts = root.find(".//d:ObjectCounts", namespace)
    if object_counts is not None:
        counts = {node.tag.split("}")[-1]: int(node.text or 0) for node in object_counts}
    return data_version, int(total_node.text), counts


def _source_column_map(table: Table) -> dict[str, Column]:
    return {column.source_name: column for column in table.columns}


def _normalized_column(table: Table, source_name: str) -> str:
    columns = _source_column_map(table)
    try:
        return columns[source_name].sqlite_name
    except KeyError as exc:
        raise ConversionError(f"Unknown column {source_name!r} on {table.source_full_name}") from exc


def _datetime2_7(value: object) -> str:
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?",
        str(value),
    )
    if match is None:
        raise ConversionError(f"Invalid datetime2(7) value: {value!r}")
    fraction = (match.group(3) or "").ljust(7, "0")
    return f"{match.group(1)} {match.group(2)}.{fraction}"


def _computed_value(column: Column, values: dict[str, object]):
    name = column.source_full_name
    if name == "[Application].[People].[SearchName]":
        return f"{values.get('PreferredName') or ''} {values.get('FullName') or ''}"
    if name in {
        "[Purchasing].[SupplierTransactions].[IsFinalized]",
        "[Sales].[CustomerTransactions].[IsFinalized]",
    }:
        return int(values.get("FinalizationDate") is not None)
    if name == "[Warehouse].[StockItems].[SearchDetails]":
        return f"{values.get('StockItemName') or ''} {values.get('MarketingComments') or ''}"

    json_fields = {
        "[Application].[People].[OtherLanguages]": ("CustomFields", "OtherLanguages", True),
        "[Warehouse].[StockItems].[Tags]": ("CustomFields", "Tags", True),
        "[Sales].[Invoices].[ConfirmedDeliveryTime]": (
            "ReturnedDeliveryData",
            "DeliveredWhen",
            False,
        ),
        "[Sales].[Invoices].[ConfirmedReceivedBy]": (
            "ReturnedDeliveryData",
            "ReceivedBy",
            False,
        ),
    }
    if name not in json_fields:
        raise ConversionError(
            f"Unsupported computed base-table column {name}; expression={column.expression!r}"
        )
    source_name, property_name, preserve_json = json_fields[name]
    raw = values.get(source_name)
    if raw is None:
        return None
    try:
        value = json.loads(str(raw)).get(property_name)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ConversionError(f"Invalid JSON used by computed column {name}") from exc
    if value is None:
        return None
    if preserve_json:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if name == "[Sales].[Invoices].[ConfirmedDeliveryTime]":
        # CONVERT(datetime2(7), JSON_VALUE(...)) in the source expression.
        return _datetime2_7(value)
    return str(value) if isinstance(value, (str, int, float, bool)) else None


def _output_columns(table: Table) -> list[tuple[str, str, bool]]:
    """Return (SQLite name, type, nullable) including geography derivatives."""
    result: list[tuple[str, str, bool]] = []
    for column in table.columns:
        result.append((column.sqlite_name, column.sqlite_type, column.nullable or column.computed))
        if column.sql_type == "geography":
            result.append((f"{column.sqlite_name}__wkt", "TEXT", True))
            result.append((f"{column.sqlite_name}__srid", "INTEGER", True))
    return result


def _create_table_sql(table: Table, table_lookup: dict[str, Table]) -> str:
    definitions: list[str] = []
    for name, sqlite_type, nullable in _output_columns(table):
        definition = f"{quote_identifier(name)} {sqlite_type}"
        if not nullable:
            definition += " NOT NULL"
        definitions.append(definition)
    if table.primary_key:
        columns = ", ".join(
            quote_identifier(_normalized_column(table, name)) for name in table.primary_key
        )
        definitions.append(f"PRIMARY KEY ({columns})")
    for unique_columns in table.unique_constraints:
        columns = ", ".join(
            quote_identifier(_normalized_column(table, name)) for name in unique_columns
        )
        definitions.append(f"UNIQUE ({columns})")
    for foreign_key in table.foreign_keys:
        target = table_lookup.get(foreign_key.foreign_table_full_name)
        if target is None:
            raise ConversionError(
                f"Foreign-key target is not a base table: {foreign_key.foreign_table_full_name}"
            )
        local = ", ".join(
            quote_identifier(_normalized_column(table, name)) for name in foreign_key.columns
        )
        remote = ", ".join(
            quote_identifier(_normalized_column(target, name))
            for name in foreign_key.foreign_columns
        )
        definitions.append(
            f"FOREIGN KEY ({local}) REFERENCES {quote_identifier(target.sqlite_name)} ({remote})"
        )
    body = ",\n  ".join(definitions)
    return f"CREATE TABLE {quote_identifier(table.sqlite_name)} (\n  {body}\n)"


def _create_metadata_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE _meta_source (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE _meta_identifier_map (
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_column TEXT,
          sqlite_table TEXT NOT NULL,
          sqlite_column TEXT,
          sql_type TEXT,
          sqlite_type TEXT,
          is_computed INTEGER NOT NULL,
          ordinal INTEGER
        );
        CREATE TABLE _meta_import_counts (
          source_schema TEXT NOT NULL,
          source_table TEXT NOT NULL,
          sqlite_table TEXT PRIMARY KEY,
          row_count INTEGER NOT NULL,
          bcp_parts INTEGER NOT NULL
        );
        """
    )


def _row_for_sqlite(table: Table, row: tuple) -> tuple:
    simple = table.simple_columns
    source_values = {column.source_name: value for column, value in zip(simple, row, strict=True)}
    output: list[object] = []
    for column in table.columns:
        if column.computed:
            output.append(_computed_value(column, source_values))
            continue
        value = source_values[column.source_name]
        output.append(value)
        if column.sql_type == "geography":
            if value is None:
                output.extend((None, None))
            else:
                try:
                    wkt, srid = decode_geography(value)
                except ValueError as exc:
                    raise ConversionError(
                        f"Could not decode geography value in {column.source_full_name}: {exc}"
                    ) from exc
                output.extend((wkt, srid))
    return tuple(output)


def _bcp_parts(archive: zipfile.ZipFile, table: Table) -> list[str]:
    directory = urllib.parse.quote(f"{table.schema}.{table.source_name}", safe=".")
    prefix = f"Data/{directory}/"
    return sorted(
        name for name in archive.namelist() if name.startswith(prefix) and name.endswith(".BCP")
    )


def _load_table(
    connection: sqlite3.Connection,
    archive: zipfile.ZipFile,
    table: Table,
    batch_size: int,
) -> tuple[int, int]:
    parts = _bcp_parts(archive, table)
    output_names = [name for name, _, _ in _output_columns(table)]
    placeholders = ", ".join("?" for _ in output_names)
    insert_sql = (
        f"INSERT INTO {quote_identifier(table.sqlite_name)} "
        f"({', '.join(quote_identifier(name) for name in output_names)}) VALUES ({placeholders})"
    )
    batch: list[tuple] = []
    row_count = 0
    for part in parts:
        try:
            with archive.open(part, "r") as stream:
                for row in iter_rows(stream, table.simple_columns):
                    batch.append(_row_for_sqlite(table, row))
                    if len(batch) >= batch_size:
                        connection.executemany(insert_sql, batch)
                        row_count += len(batch)
                        batch.clear()
        except (BCPFormatError, EOFError) as exc:
            raise ConversionError(f"BCP boundary error in {part} after {row_count} rows: {exc}") from exc
    if batch:
        connection.executemany(insert_sql, batch)
        row_count += len(batch)
    return row_count, len(parts)


def _create_indexes(connection: sqlite3.Connection, model: DatabaseModel) -> int:
    used_names: set[str] = set()
    count = 0
    for table in model.tables:
        for index in table.indexes:
            base = f"ix__{table.sqlite_name}__{snake_case(index.source_name)}"
            name = base
            suffix = 2
            while name in used_names:
                name = f"{base}__{suffix}"
                suffix += 1
            used_names.add(name)
            columns = ", ".join(
                f"{quote_identifier(_normalized_column(table, source_name))}"
                + (" DESC" if descending else "")
                for source_name, descending in index.columns
            )
            unique = "UNIQUE " if index.unique else ""
            connection.execute(
                f"CREATE {unique}INDEX {quote_identifier(name)} "
                f"ON {quote_identifier(table.sqlite_name)} ({columns})"
            )
            count += 1
    return count


def _populate_identifier_map(connection: sqlite3.Connection, model: DatabaseModel) -> None:
    rows: list[tuple] = []
    for table in model.tables:
        rows.append(
            (
                table.schema,
                table.source_name,
                None,
                table.sqlite_name,
                None,
                None,
                None,
                0,
                None,
            )
        )
        for column in table.columns:
            rows.append(
                (
                    table.schema,
                    table.source_name,
                    column.source_name,
                    table.sqlite_name,
                    column.sqlite_name,
                    column.sql_declaration,
                    column.sqlite_type,
                    int(column.computed),
                    column.ordinal,
                )
            )
            if column.sql_type == "geography":
                for suffix, sqlite_type in (("__wkt", "TEXT"), ("__srid", "INTEGER")):
                    rows.append(
                        (
                            table.schema,
                            table.source_name,
                            f"{column.source_name}{suffix}",
                            table.sqlite_name,
                            f"{column.sqlite_name}{suffix}",
                            "derived from geography",
                            sqlite_type,
                            1,
                            column.ordinal,
                        )
                    )
    connection.executemany(
        "INSERT INTO _meta_identifier_map VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )


def _validate_connection(
    connection: sqlite3.Connection,
    spec: SourceSpec,
    *,
    count_rows: bool,
) -> dict[str, object]:
    imported = connection.execute(
        "SELECT sqlite_table, row_count FROM _meta_import_counts ORDER BY sqlite_table"
    ).fetchall()
    if len(imported) != spec.expected_tables:
        raise ConversionError(
            f"{spec.key}: expected {spec.expected_tables} tables, found {len(imported)}"
        )
    metadata_total = sum(row[1] for row in imported)
    if metadata_total != spec.expected_rows:
        raise ConversionError(
            f"{spec.key}: expected {spec.expected_rows:,} rows, metadata has {metadata_total:,}"
        )
    if count_rows:
        for table_name, expected in imported:
            actual = connection.execute(
                f"SELECT count(*) FROM {quote_identifier(table_name)}"
            ).fetchone()[0]
            if actual != expected:
                raise ConversionError(
                    f"{table_name}: expected {expected:,} rows, found {actual:,}"
                )
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise ConversionError(f"SQLite integrity_check failed: {integrity[:10]}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ConversionError(
            f"SQLite foreign_key_check found {len(foreign_key_errors)} violations: "
            f"{foreign_key_errors[:10]}"
        )
    return {
        "table_count": len(imported),
        "row_count": metadata_total,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def build_database(
    spec: SourceSpec,
    bacpac_path: Path,
    output_path: Path,
    *,
    force: bool,
    batch_size: int,
) -> dict[str, object]:
    if output_path.exists() and not force:
        raise ConversionError(f"Output already exists (use --force to replace it): {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    connection: sqlite3.Connection | None = None
    try:
        with zipfile.ZipFile(bacpac_path) as archive:
            try:
                model_xml = archive.read("model.xml")
                origin_xml = archive.read("Origin.xml")
            except KeyError as exc:
                raise ConversionError(f"{bacpac_path} is missing required BACPAC metadata") from exc
            stream_version, embedded_total, object_counts = _origin_metadata(origin_xml)
            if stream_version != "2.0.0.0":
                raise ConversionError(
                    f"Unsupported BACPAC Data stream version {stream_version!r}; expected '2.0.0.0'"
                )
            if embedded_total != spec.expected_rows:
                raise ConversionError(
                    f"BACPAC embedded row total is {embedded_total:,}; expected {spec.expected_rows:,}"
                )
            model = parse_model(model_xml, object_counts)
            if len(model.tables) != spec.expected_tables:
                raise ConversionError(
                    f"BACPAC has {len(model.tables)} base tables; expected {spec.expected_tables}"
                )

            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-262144")
            connection.execute("PRAGMA user_version=1")
            _create_metadata_tables(connection)
            table_lookup = {table.source_full_name: table for table in model.tables}
            for table in model.tables:
                connection.execute(_create_table_sql(table, table_lookup))
            _populate_identifier_map(connection, model)
            source_metadata = {
                "converter_version": __version__,
                "dataset": spec.key,
                "source_url": spec.url,
                "source_filename": spec.bacpac_name,
                "source_sha256": spec.sha256,
                "data_stream_version": stream_version,
                "embedded_row_count": str(embedded_total),
                "source_object_counts": json.dumps(object_counts, sort_keys=True),
            }
            connection.executemany(
                "INSERT INTO _meta_source(key, value) VALUES (?, ?)", source_metadata.items()
            )

            total_rows = 0
            for table in model.tables:
                row_count, part_count = _load_table(connection, archive, table, batch_size)
                total_rows += row_count
                connection.execute(
                    "INSERT INTO _meta_import_counts VALUES (?, ?, ?, ?, ?)",
                    (table.schema, table.source_name, table.sqlite_name, row_count, part_count),
                )
                print(f"  {table.sqlite_name}: {row_count:,} rows ({part_count} BCP parts)", flush=True)
            if total_rows != embedded_total:
                raise ConversionError(
                    f"Decoded {total_rows:,} rows, but Origin.xml declares {embedded_total:,}"
                )
            connection.commit()

            index_count = _create_indexes(connection, model)
            connection.commit()
            connection.execute("ANALYZE")
            connection.execute("PRAGMA optimize")
            connection.commit()
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            validation = _validate_connection(connection, spec, count_rows=False)
            validation["index_count"] = index_count
            validation["primary_key_count"] = sum(bool(table.primary_key) for table in model.tables)
            validation["unique_constraint_count"] = sum(
                len(table.unique_constraints) for table in model.tables
            )
            validation["foreign_key_count"] = sum(len(table.foreign_keys) for table in model.tables)
            connection.close()
            connection = None

        os.replace(temporary, output_path)
        size = output_path.stat().st_size
        print(f"Published {output_path} ({size / 1024 / 1024:.1f} MiB)", flush=True)
        return {
            "dataset": spec.key,
            "source_url": spec.url,
            "source_filename": spec.bacpac_name,
            "source_sha256": spec.sha256,
            "output_file": output_path.name,
            "output_bytes": size,
            **validation,
        }
    except (sqlite3.Error, zipfile.BadZipFile) as exc:
        raise ConversionError(str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def build_all(
    output_dir: Path,
    source_dir: Path,
    *,
    offline: bool,
    force: bool,
    batch_size: int = 10_000,
) -> Path:
    if batch_size < 1:
        raise ConversionError("--batch-size must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = [output_dir / spec.output_name for spec in SOURCES if (output_dir / spec.output_name).exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise ConversionError(f"Output already exists (use --force to replace): {joined}")

    results: list[dict[str, object]] = []
    for spec in SOURCES:
        print(f"Acquiring {spec.bacpac_name}", flush=True)
        source = acquire_source(spec, source_dir, offline)
        print(f"Building {spec.output_name}", flush=True)
        results.append(
            build_database(
                spec,
                source,
                output_dir / spec.output_name,
                force=force,
                batch_size=batch_size,
            )
        )

    manifest = {
        "manifest_version": 1,
        "converter_version": __version__,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python": sys.version.split()[0],
            "python_implementation": platform.python_implementation(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "databases": results,
    }
    manifest_path = output_dir / "build_manifest.json"
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".build_manifest.", suffix=".tmp", dir=output_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, manifest_path)
        os.chmod(manifest_path, 0o644)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return manifest_path


def _spec_from_database(connection: sqlite3.Connection, path: Path) -> SourceSpec:
    try:
        row = connection.execute("SELECT value FROM _meta_source WHERE key='dataset'").fetchone()
    except sqlite3.Error as exc:
        raise ConversionError(f"{path} is not a WWI SQLite database: {exc}") from exc
    if row is None or row[0] not in SOURCE_BY_KEY:
        raise ConversionError(f"{path} has no recognized dataset metadata")
    return SOURCE_BY_KEY[row[0]]


def _verify_representative_types(connection: sqlite3.Connection) -> None:
    mappings = connection.execute(
        """
        SELECT sqlite_table, sqlite_column, sql_type, sqlite_type, is_computed
        FROM _meta_identifier_map
        WHERE sqlite_column IS NOT NULL
        """
    ).fetchall()
    table_columns: dict[str, set[str]] = {}
    for table, column, _, _, _ in mappings:
        table_columns.setdefault(table, set()).add(column)

    multipart = connection.execute(
        "SELECT max(bcp_parts) FROM _meta_import_counts"
    ).fetchone()[0]
    if multipart is None or multipart <= 1:
        raise ConversionError("No multipart BCP table was recorded")

    found_max_text = False
    found_binary = False

    for table, column, sql_type, sqlite_type, is_computed in mappings:
        if is_computed or sql_type == "derived from geography":
            continue
        if sql_type and sql_type.startswith("decimal"):
            actual = connection.execute(
                f"SELECT typeof({quote_identifier(column)}), {quote_identifier(column)} "
                f"FROM {quote_identifier(table)} "
                f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT 1"
            ).fetchone()
            if actual and actual[0] != "text":
                raise ConversionError(f"Decimal affinity check failed for {table}.{column}")
            if actual:
                scale_match = re.search(r",(\d+)\)$", sql_type)
                scale = int(scale_match.group(1)) if scale_match else 0
                pattern = rf"^-?\d+\.\d{{{scale}}}$" if scale else r"^-?\d+$"
                if re.fullmatch(pattern, actual[1]) is None:
                    raise ConversionError(f"Decimal canonical-form check failed for {table}.{column}")
        elif sql_type == "datetime2(7)":
            actual = connection.execute(
                f"SELECT {quote_identifier(column)} FROM {quote_identifier(table)} "
                f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT 1"
            ).fetchone()
            if actual and re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{7}", actual[0]
            ) is None:
                raise ConversionError(f"datetime2 format check failed for {table}.{column}")
        elif sql_type == "nvarchar(max)":
            maximum = connection.execute(
                f"SELECT max(length({quote_identifier(column)})) FROM {quote_identifier(table)}"
            ).fetchone()[0]
            found_max_text = found_max_text or bool(maximum)
        elif sql_type and sql_type.startswith("varbinary"):
            actual = connection.execute(
                f"SELECT typeof({quote_identifier(column)}) FROM {quote_identifier(table)} "
                f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT 1"
            ).fetchone()
            if actual and actual[0] != "blob":
                raise ConversionError(f"Binary affinity check failed for {table}.{column}")
            found_binary = found_binary or bool(actual)
        elif sql_type == "geography":
            required = {column, f"{column}__wkt", f"{column}__srid"}
            if not required <= table_columns[table]:
                raise ConversionError(f"Missing geography derivatives for {table}.{column}")
            actual = connection.execute(
                f"SELECT typeof({quote_identifier(column)}), "
                f"{quote_identifier(column + '__wkt')}, "
                f"typeof({quote_identifier(column + '__srid')}) "
                f"FROM {quote_identifier(table)} WHERE {quote_identifier(column)} IS NOT NULL LIMIT 1"
            ).fetchone()
            if actual and (actual[0] != "blob" or not actual[1] or actual[2] != "integer"):
                raise ConversionError(f"Geography check failed for {table}.{column}")

    computed = {
        (table, column)
        for table, column, _, _, is_computed in mappings
        if is_computed and not column.endswith(("__wkt", "__srid"))
    }
    if "application__people" in table_columns:
        if not found_max_text:
            raise ConversionError("No non-null OLTP nvarchar(max) record was available for validation")
        if not found_binary:
            raise ConversionError("No non-null OLTP binary record was available for validation")
        expected_computed = {
            ("application__people", "search_name"),
            ("application__people", "other_languages"),
            ("purchasing__supplier_transactions", "is_finalized"),
            ("sales__customer_transactions", "is_finalized"),
            ("sales__invoices", "confirmed_delivery_time"),
            ("sales__invoices", "confirmed_received_by"),
            ("warehouse__stock_items", "tags"),
            ("warehouse__stock_items", "search_details"),
        }
        if computed != expected_computed:
            raise ConversionError(
                f"Materialized computed-column set differs from the expected eight: {sorted(computed)}"
            )
        mismatch = connection.execute(
            """
            SELECT count(*) FROM application__people
            WHERE search_name <> coalesce(preferred_name, '') || ' ' || coalesce(full_name, '')
            """
        ).fetchone()[0]
        if mismatch:
            raise ConversionError(f"Found {mismatch} incorrect computed search_name values")
        for table in ("purchasing__supplier_transactions", "sales__customer_transactions"):
            mismatch = connection.execute(
                f"SELECT count(*) FROM {quote_identifier(table)} "
                "WHERE is_finalized <> (finalization_date IS NOT NULL)"
            ).fetchone()[0]
            if mismatch:
                raise ConversionError(f"Found {mismatch} incorrect is_finalized values in {table}")
        nullable = connection.execute(
            "SELECT 1 FROM sales__orders WHERE comments IS NULL LIMIT 1"
        ).fetchone()
        if nullable is None:
            raise ConversionError("No representative nullable record was found")
        invoice = connection.execute(
            """
            SELECT returned_delivery_data, confirmed_delivery_time, confirmed_received_by
            FROM sales__invoices WHERE confirmed_delivery_time IS NOT NULL LIMIT 1
            """
        ).fetchone()
        if invoice is None:
            raise ConversionError("No computed delivery-confirmation record was found")
        delivery = json.loads(invoice[0])
        expected_time = _datetime2_7(delivery["DeliveredWhen"])
        if invoice[1] != expected_time or invoice[2] != delivery["ReceivedBy"]:
            raise ConversionError("Computed delivery-confirmation values do not match source JSON")


def verify_database(path: Path, *, count_rows: bool = True) -> dict[str, object]:
    if not path.is_file():
        raise ConversionError(f"Database does not exist: {path}")
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        spec = _spec_from_database(connection, path)
        result = _validate_connection(connection, spec, count_rows=count_rows)
        _verify_representative_types(connection)
        result.update({"dataset": spec.key, "path": str(path), "bytes": path.stat().st_size})
        return result
    finally:
        connection.close()


def verify_all(paths: list[Path]) -> list[dict[str, object]]:
    results = []
    for path in paths:
        result = verify_database(path)
        print(
            f"Verified {path}: {result['table_count']} tables, "
            f"{result['row_count']:,} rows, integrity ok, no foreign-key violations",
            flush=True,
        )
        results.append(result)
    return results
