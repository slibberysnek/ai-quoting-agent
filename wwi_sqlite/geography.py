"""Decode SQL Server's serialized geography UDT into WKT and SRID."""

from __future__ import annotations

from dataclasses import dataclass
import struct


@dataclass(slots=True)
class _Figure:
    attribute: int
    point_offset: int


@dataclass(slots=True)
class _Shape:
    parent_offset: int
    figure_offset: int
    shape_type: int


class _Cursor:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise ValueError("Truncated geography serialization")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]


def _number(value: float) -> str:
    if value == 0:
        return "0"
    return format(value, ".15g")


def decode_geography(data: bytes) -> tuple[str, int]:
    """Return ``(WKT, SRID)`` for a SQL Server geography serialization."""
    cursor = _Cursor(data)
    srid = cursor.i32()
    version = cursor.u8()
    flags = cursor.u8()
    if version not in {1, 2}:
        raise ValueError(f"Unsupported geography serialization version {version}")

    has_z = bool(flags & 0x01)
    has_m = bool(flags & 0x02)
    single_point = bool(flags & 0x08)
    single_line = bool(flags & 0x10)
    if single_point:
        point_count = 1
    elif single_line:
        point_count = 2
    else:
        point_count = cursor.u32()

    # SQL Server geography stores latitude before longitude; WKT uses X/Y.
    points = [(cursor.f64(), cursor.f64()) for _ in range(point_count)]
    z_values = [cursor.f64() for _ in range(point_count)] if has_z else []
    m_values = [cursor.f64() for _ in range(point_count)] if has_m else []

    if single_point or single_line:
        figures = [_Figure(1, 0)]
    else:
        figures = [_Figure(cursor.u8(), cursor.u32()) for _ in range(cursor.u32())]

    if single_point:
        shapes = [_Shape(-1, 0, 1)]
    elif single_line:
        shapes = [_Shape(-1, 0, 2)]
    else:
        shapes = [_Shape(cursor.i32(), cursor.i32(), cursor.u8()) for _ in range(cursor.u32())]

    if version == 2 and cursor.offset < len(data):
        segment_count = cursor.u32()
        cursor.take(segment_count)
    if cursor.offset != len(data):
        raise ValueError(f"Geography serialization has {len(data) - cursor.offset} leftover bytes")
    if not shapes:
        raise ValueError("Geography serialization contains no root shape")

    dimension = ""
    if has_z and has_m:
        dimension = " ZM"
    elif has_z:
        dimension = " Z"
    elif has_m:
        dimension = " M"

    def coordinate(index: int) -> str:
        latitude, longitude = points[index]
        values = [_number(longitude), _number(latitude)]
        if has_z:
            values.append(_number(z_values[index]))
        if has_m:
            values.append(_number(m_values[index]))
        return " ".join(values)

    def figure_coordinates(index: int) -> list[str]:
        start = figures[index].point_offset
        end = figures[index + 1].point_offset if index + 1 < len(figures) else len(points)
        return [coordinate(i) for i in range(start, end)]

    def shape_figure_indexes(index: int) -> list[int]:
        start = shapes[index].figure_offset
        if start < 0:
            return []
        later = [shape.figure_offset for shape in shapes[index + 1 :] if shape.figure_offset > start]
        end = min(later) if later else len(figures)
        return list(range(start, end))

    def children(index: int) -> list[int]:
        return [i for i, shape in enumerate(shapes) if shape.parent_offset == index]

    names = {
        1: "POINT",
        2: "LINESTRING",
        3: "POLYGON",
        4: "MULTIPOINT",
        5: "MULTILINESTRING",
        6: "MULTIPOLYGON",
        7: "GEOMETRYCOLLECTION",
        11: "FULLGLOBE",
    }

    def body(index: int, nested: bool = False) -> str:
        shape = shapes[index]
        shape_type = shape.shape_type
        name = names.get(shape_type)
        if name is None:
            raise ValueError(f"Unsupported curved geography shape type {shape_type}")
        if shape_type == 11:
            return "FULLGLOBE"
        figure_indexes = shape_figure_indexes(index)
        child_indexes = children(index)
        if shape_type == 1:
            if not figure_indexes:
                return "EMPTY" if nested else f"POINT{dimension} EMPTY"
            coords = figure_coordinates(figure_indexes[0])
            content = coords[0]
        elif shape_type == 2:
            if not figure_indexes:
                return "EMPTY" if nested else f"LINESTRING{dimension} EMPTY"
            content = ", ".join(figure_coordinates(figure_indexes[0]))
        elif shape_type == 3:
            if not figure_indexes:
                return "EMPTY" if nested else f"POLYGON{dimension} EMPTY"
            rings: list[str] = []
            for figure_index in figure_indexes:
                coords = figure_coordinates(figure_index)
                if coords and coords[0] != coords[-1]:
                    coords.append(coords[0])
                rings.append("(" + ", ".join(coords) + ")")
            content = ", ".join(rings)
        elif shape_type in {4, 5, 6}:
            if not child_indexes:
                return "EMPTY" if nested else f"{name}{dimension} EMPTY"
            pieces = []
            for child_index in child_indexes:
                child = body(child_index, nested=True)
                pieces.append(child if child == "EMPTY" else f"({child})")
            content = ", ".join(pieces)
        else:  # GEOMETRYCOLLECTION
            if not child_indexes:
                return "EMPTY" if nested else f"GEOMETRYCOLLECTION{dimension} EMPTY"
            content = ", ".join(body(child_index, nested=False) for child_index in child_indexes)

        if nested:
            return content
        return f"{name}{dimension} ({content})"

    root_indexes = [i for i, shape in enumerate(shapes) if shape.parent_offset == -1]
    if len(root_indexes) != 1:
        raise ValueError(f"Expected one root geography shape, found {len(root_indexes)}")
    return body(root_indexes[0]), srid

