"""Streaming reader for the native bulk streams inside the target BACPACs."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import io
import struct
from typing import BinaryIO, Iterator

from .model import Column


class BCPFormatError(ValueError):
    pass


def _read_exact(reader: BinaryIO, size: int) -> bytes:
    value = reader.read(size)
    if len(value) != size:
        raise BCPFormatError(f"Truncated BCP field: expected {size} bytes, received {len(value)}")
    return value


def _nullable_fixed(reader: BinaryIO, size: int, nullable: bool) -> bytes | None:
    if nullable:
        length = _read_exact(reader, 1)[0]
        if length == 0xFF:
            return None
        if length != size:
            raise BCPFormatError(f"Expected a {size}-byte native value, length prefix was {length}")
    return _read_exact(reader, size)


def _read_plp(reader: BinaryIO) -> bytes | None:
    total = struct.unpack("<q", _read_exact(reader, 8))[0]
    if total == -1:
        return None
    if total < -2:
        raise BCPFormatError(f"Invalid PLP length {total}")
    if total >= 0:
        return _read_exact(reader, total)
    chunks: list[bytes] = []
    total_read = 0
    while True:
        chunk_size = struct.unpack("<I", _read_exact(reader, 4))[0]
        if chunk_size == 0:
            return b"".join(chunks)
        chunks.append(_read_exact(reader, chunk_size))
        total_read += chunk_size
        if total_read > (1 << 31):
            raise BCPFormatError("Unreasonably large chunked PLP value")


def _format_decimal(raw: bytes, scale: int | None) -> str:
    if len(raw) != 19:
        raise BCPFormatError(f"Expected a 19-byte decimal payload, received {len(raw)}")
    encoded_scale = raw[1]
    if scale is not None and encoded_scale != scale:
        raise BCPFormatError(f"Decimal scale mismatch: model={scale}, stream={encoded_scale}")
    magnitude = int.from_bytes(raw[3:], "little", signed=False)
    sign = "" if raw[2] else "-"
    if encoded_scale == 0:
        return sign + str(magnitude)
    digits = str(magnitude).rjust(encoded_scale + 1, "0")
    return f"{sign}{digits[:-encoded_scale]}.{digits[-encoded_scale:]}"


def read_value(reader: BinaryIO, column: Column):
    sql_type = column.sql_type
    if sql_type == "int":
        raw = _nullable_fixed(reader, 4, column.nullable)
        return None if raw is None else int.from_bytes(raw, "little", signed=True)
    if sql_type == "bigint":
        raw = _nullable_fixed(reader, 8, column.nullable)
        return None if raw is None else int.from_bytes(raw, "little", signed=True)
    if sql_type == "bit":
        raw = _nullable_fixed(reader, 1, True)
        if raw is None:
            return None
        if raw[0] not in {0, 1}:
            raise BCPFormatError(f"Invalid bit value {raw[0]}")
        return raw[0]
    if sql_type == "date":
        raw = _nullable_fixed(reader, 3, column.nullable)
        if raw is None:
            return None
        days = int.from_bytes(raw, "little", signed=False)
        return (date(1, 1, 1) + timedelta(days=days)).isoformat()
    if sql_type == "datetime2":
        scale = column.scale
        if scale is None or not 0 <= scale <= 7:
            raise BCPFormatError(f"Unsupported datetime2 scale {scale}")
        time_size = 3 if scale <= 2 else 4 if scale <= 4 else 5
        raw = _nullable_fixed(reader, time_size + 3, column.nullable)
        if raw is None:
            return None
        time_units = int.from_bytes(raw[:time_size], "little", signed=False)
        days = int.from_bytes(raw[time_size:], "little", signed=False)
        day = date(1, 1, 1) + timedelta(days=days)
        ticks = time_units * (10 ** (7 - scale))
        hours, remainder = divmod(ticks, 36_000_000_000)
        minutes, remainder = divmod(remainder, 600_000_000)
        seconds, fraction = divmod(remainder, 10_000_000)
        return f"{day.isoformat()} {hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:07d}"
    if sql_type == "decimal":
        length = _read_exact(reader, 1)[0]
        if length == 0xFF:
            return None
        return _format_decimal(_read_exact(reader, length), column.scale)
    if sql_type in {"nvarchar", "sysname"}:
        if column.is_max:
            raw = _read_plp(reader)
        else:
            length = struct.unpack("<H", _read_exact(reader, 2))[0]
            raw = None if length == 0xFFFF else _read_exact(reader, length)
        if raw is None:
            return None
        try:
            return raw.decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise BCPFormatError(f"Invalid UTF-16LE text in {column.source_full_name}") from exc
    if sql_type in {"varbinary", "geography"}:
        if not column.is_max and sql_type == "varbinary":
            length = struct.unpack("<H", _read_exact(reader, 2))[0]
            return None if length == 0xFFFF else _read_exact(reader, length)
        return _read_plp(reader)
    raise BCPFormatError(f"Unsupported SQL type {sql_type!r}")


def iter_rows(stream: BinaryIO, columns: list[Column]) -> Iterator[tuple]:
    reader = stream if isinstance(stream, io.BufferedReader) else io.BufferedReader(stream)
    while reader.peek(1):
        try:
            yield tuple(read_value(reader, column) for column in columns)
        except BCPFormatError:
            raise
        except Exception as exc:
            raise BCPFormatError(str(exc)) from exc

