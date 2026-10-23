"""Command-line interface for :mod:`wwi_sqlite`."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .converter import ConversionError, SOURCES, build_all, verify_all


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m wwi_sqlite",
        description="Convert Wide World Importers Standard BACPACs to SQLite.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="download and build both SQLite databases")
    build.add_argument("--output-dir", type=Path, default=Path("data/sqlite"))
    build.add_argument(
        "--source-dir",
        type=Path,
        default=Path("data/source"),
        help="BACPAC cache directory (default: data/source)",
    )
    build.add_argument(
        "--offline",
        action="store_true",
        help="do not download; require verified BACPACs in --source-dir",
    )
    build.add_argument(
        "--force", action="store_true", help="atomically replace existing SQLite outputs"
    )
    build.add_argument("--batch-size", type=int, default=10_000, help=argparse.SUPPRESS)

    verify = subparsers.add_parser("verify", help="validate generated SQLite databases")
    verify.add_argument("databases", nargs="*", type=Path)
    verify.add_argument("--output-dir", type=Path, default=Path("data/sqlite"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_all(
                args.output_dir,
                args.source_dir,
                offline=args.offline,
                force=args.force,
                batch_size=args.batch_size,
            )
            print(f"Build complete. Manifest: {manifest}")
        else:
            paths = args.databases or [args.output_dir / source.output_name for source in SOURCES]
            verify_all(paths)
    except (ConversionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

