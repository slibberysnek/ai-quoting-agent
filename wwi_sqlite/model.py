"""Parse the subset of the DAC model used by Wide World Importers."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import xml.etree.ElementTree as ET


DAC_NS = "http://schemas.microsoft.com/sqlserver/dac/Serialization/2012/02"
Q = lambda name: f"{{{DAC_NS}}}{name}"
_BRACKETED = re.compile(r"\[([^]]+)\]")


def identifier_parts(name: str) -> list[str]:
    parts = _BRACKETED.findall(name)
    if not parts:
        raise ValueError(f"Unrecognized DAC identifier: {name!r}")
    return parts


def snake_case(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    if not value:
        raise ValueError("An identifier normalized to an empty string")
    if value[0].isdigit():
        value = "_" + value
    return value


def sqlite_table_name(schema: str, table: str) -> str:
    return f"{snake_case(schema)}__{snake_case(table)}"


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _property(element: ET.Element, name: str, default: str | None = None) -> str | None:
    for prop in element.findall(Q("Property")):
        if prop.get("Name") == name:
            if "Value" in prop.attrib:
                return prop.get("Value")
            value = prop.find(Q("Value"))
            return value.text if value is not None else default
    return default


def _relationship(element: ET.Element, name: str) -> ET.Element | None:
    for relation in element.findall(Q("Relationship")):
        if relation.get("Name") == name:
            return relation
    return None


def _relationship_elements(element: ET.Element, name: str) -> list[ET.Element]:
    relation = _relationship(element, name)
    if relation is None:
        return []
    result: list[ET.Element] = []
    for entry in relation.findall(Q("Entry")):
        child = entry.find(Q("Element"))
        if child is not None:
            result.append(child)
    return result


def _relationship_references(element: ET.Element, name: str) -> list[str]:
    relation = _relationship(element, name)
    if relation is None:
        return []
    return [
        ref.get("Name", "")
        for entry in relation.findall(Q("Entry"))
        for ref in entry.findall(Q("References"))
        if ref.get("Name")
    ]


@dataclass(slots=True)
class Column:
    source_full_name: str
    source_name: str
    sqlite_name: str
    sql_type: str
    nullable: bool
    ordinal: int
    length: int | None = None
    is_max: bool = False
    precision: int | None = None
    scale: int | None = None
    computed: bool = False
    expression: str | None = None

    @property
    def sqlite_type(self) -> str:
        if self.computed:
            key = self.source_full_name
            if key.endswith(".[IsFinalized]"):
                return "INTEGER"
            return "TEXT"
        if self.sql_type in {"int", "bigint", "bit"}:
            return "INTEGER"
        if self.sql_type in {"date", "datetime2", "decimal", "nvarchar", "sysname"}:
            return "TEXT"
        if self.sql_type in {"varbinary", "geography"}:
            return "BLOB"
        raise ValueError(f"Unsupported SQL type {self.sql_type!r} on {self.source_full_name}")

    @property
    def sql_declaration(self) -> str:
        if self.computed:
            return "computed"
        if self.sql_type == "nvarchar":
            suffix = "max" if self.is_max else str(self.length)
            return f"nvarchar({suffix})"
        if self.sql_type == "varbinary":
            suffix = "max" if self.is_max else str(self.length)
            return f"varbinary({suffix})"
        if self.sql_type == "decimal":
            return f"decimal({self.precision},{self.scale})"
        if self.sql_type == "datetime2":
            return f"datetime2({self.scale})"
        return self.sql_type


@dataclass(slots=True)
class ForeignKey:
    columns: list[str]
    foreign_table_full_name: str
    foreign_columns: list[str]


@dataclass(slots=True)
class Index:
    source_name: str
    columns: list[tuple[str, bool]]
    unique: bool = False


@dataclass(slots=True)
class Table:
    source_full_name: str
    schema: str
    source_name: str
    sqlite_name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    unique_constraints: list[list[str]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)

    @property
    def simple_columns(self) -> list[Column]:
        return [column for column in self.columns if not column.computed]


@dataclass(slots=True)
class DatabaseModel:
    tables: list[Table]
    object_counts: dict[str, int]


_ALLOWED_TYPES = {
    "int",
    "bigint",
    "bit",
    "date",
    "datetime2",
    "decimal",
    "nvarchar",
    "sysname",
    "varbinary",
    "geography",
}


def _parse_column(element: ET.Element, ordinal: int) -> Column:
    full_name = element.get("Name", "")
    parts = identifier_parts(full_name)
    source_name = parts[-1]
    if element.get("Type") == "SqlComputedColumn":
        return Column(
            source_full_name=full_name,
            source_name=source_name,
            sqlite_name=snake_case(source_name),
            sql_type="computed",
            nullable=True,
            ordinal=ordinal,
            computed=True,
            expression=_property(element, "ExpressionScript"),
        )

    spec_elements = _relationship_elements(element, "TypeSpecifier")
    if len(spec_elements) != 1:
        raise ValueError(f"Expected one type specifier for {full_name}")
    spec = spec_elements[0]
    type_refs = _relationship_references(spec, "Type")
    if len(type_refs) != 1:
        raise ValueError(f"Expected one type reference for {full_name}")
    type_parts = identifier_parts(type_refs[0])
    sql_type = type_parts[-1].lower()
    if sql_type not in _ALLOWED_TYPES:
        raise ValueError(f"Unsupported SQL type {sql_type!r} on {full_name}")
    nullable = _property(element, "IsNullable", "True") != "False"
    return Column(
        source_full_name=full_name,
        source_name=source_name,
        sqlite_name=snake_case(source_name),
        sql_type=sql_type,
        nullable=nullable,
        ordinal=ordinal,
        length=int(value) if (value := _property(spec, "Length")) is not None else None,
        is_max=_property(spec, "IsMax", "False") == "True",
        precision=int(value) if (value := _property(spec, "Precision")) is not None else None,
        scale=int(value) if (value := _property(spec, "Scale")) is not None else None,
    )


def _constraint_columns(element: ET.Element) -> list[str]:
    result: list[str] = []
    for specification in _relationship_elements(element, "ColumnSpecifications"):
        refs = _relationship_references(specification, "Column")
        if len(refs) != 1:
            raise ValueError(f"Malformed indexed column specification in {element.get('Name')}")
        result.append(identifier_parts(refs[0])[-1])
    return result


def parse_model(xml_bytes: bytes, object_counts: dict[str, int] | None = None) -> DatabaseModel:
    root = ET.fromstring(xml_bytes)
    model = root.find(Q("Model"))
    if model is None:
        raise ValueError("model.xml has no DAC Model element")
    top_elements = model.findall(Q("Element"))

    tables: list[Table] = []
    table_by_full_name: dict[str, Table] = {}
    for element in top_elements:
        if element.get("Type") != "SqlTable":
            continue
        full_name = element.get("Name", "")
        parts = identifier_parts(full_name)
        if len(parts) != 2:
            raise ValueError(f"Expected a schema-qualified table name, got {full_name!r}")
        schema, source_name = parts
        column_elements = _relationship_elements(element, "Columns")
        columns = [_parse_column(column, i) for i, column in enumerate(column_elements)]
        normalized = [column.sqlite_name for column in columns]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"Column-name collision after normalization in {full_name}")
        table = Table(
            source_full_name=full_name,
            schema=schema,
            source_name=source_name,
            sqlite_name=sqlite_table_name(schema, source_name),
            columns=columns,
        )
        tables.append(table)
        table_by_full_name[full_name] = table

    if len({table.sqlite_name for table in tables}) != len(tables):
        raise ValueError("Table-name collision after normalization")

    for element in top_elements:
        element_type = element.get("Type")
        defining = _relationship_references(element, "DefiningTable")
        if element_type in {"SqlPrimaryKeyConstraint", "SqlUniqueConstraint"} and defining:
            table = table_by_full_name.get(defining[0])
            if table is None:
                continue
            columns = _constraint_columns(element)
            if element_type == "SqlPrimaryKeyConstraint":
                table.primary_key = columns
            else:
                table.unique_constraints.append(columns)
        elif element_type == "SqlForeignKeyConstraint" and defining:
            table = table_by_full_name.get(defining[0])
            foreign_table = _relationship_references(element, "ForeignTable")
            if table is None or not foreign_table:
                continue
            columns = [identifier_parts(name)[-1] for name in _relationship_references(element, "Columns")]
            foreign_columns = [
                identifier_parts(name)[-1]
                for name in _relationship_references(element, "ForeignColumns")
            ]
            if len(columns) != len(foreign_columns) or not columns:
                raise ValueError(f"Malformed foreign key {element.get('Name')}")
            table.foreign_keys.append(ForeignKey(columns, foreign_table[0], foreign_columns))
        elif element_type == "SqlIndex":
            indexed_object = _relationship_references(element, "IndexedObject")
            if not indexed_object:
                continue
            table = table_by_full_name.get(indexed_object[0])
            if table is None:
                continue
            columns: list[tuple[str, bool]] = []
            for specification in _relationship_elements(element, "ColumnSpecifications"):
                refs = _relationship_references(specification, "Column")
                if not refs:
                    continue
                descending = _property(specification, "IsDescending", "False") == "True"
                columns.append((identifier_parts(refs[0])[-1], descending))
            if columns:
                table.indexes.append(
                    Index(
                        source_name=identifier_parts(element.get("Name", ""))[-1],
                        columns=columns,
                        unique=_property(element, "IsUnique", "False") == "True",
                    )
                )

    for table in tables:
        known = {column.source_name for column in table.columns}
        referenced = set(table.primary_key)
        referenced.update(column for columns in table.unique_constraints for column in columns)
        referenced.update(column for key in table.foreign_keys for column in key.columns)
        referenced.update(column for index in table.indexes for column, _ in index.columns)
        unknown = referenced - known
        if unknown:
            raise ValueError(f"Unknown modeled columns on {table.source_full_name}: {sorted(unknown)}")

    tables.sort(key=lambda table: (table.schema, table.source_name))
    return DatabaseModel(tables=tables, object_counts=object_counts or {})

