"""Command-line entry point for manually running the local quote agent."""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from .classifier import DEFAULT_CLASSIFIER_MODEL, RequestClassifier
from .handler import DEFAULT_HANDLER_MODEL, QuoteHandler
from .models import Outcome
from .openrouter import OpenRouterClient, load_api_key
from .pipeline import QuotePipeline
from .render import render_markdown
from .tools import DEFAULT_REFERENCE_DIR, QuoteTools


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "data" / "portfolio_artifacts" / "02_inbound_quote"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify and prepare a draft response for one inbound RFQ email."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--email", type=Path, help="Specific .eml file; otherwise pick randomly")
    source.add_argument(
        "--all-fixtures",
        action="store_true",
        help="Process every .eml in --emails-dir (useful for evaluation)",
    )
    parser.add_argument("--seed", type=int, help="Seed for a reproducible random fixture choice")
    parser.add_argument("--emails-dir", type=Path, default=FIXTURE_ROOT / "emails")
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "sqlite" / "wide_world_importers.sqlite3",
    )
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "runs")
    parser.add_argument("--key-file", type=Path, help="Override the OpenRouter key file")
    parser.add_argument("--classifier-model", default=DEFAULT_CLASSIFIER_MODEL)
    parser.add_argument("--handler-model", default=DEFAULT_HANDLER_MODEL)
    return parser


def _choose_emails(args: argparse.Namespace) -> list[Path]:
    if args.email:
        return [args.email.resolve()]
    choices = sorted(args.emails_dir.resolve().glob("*.eml"))
    if not choices:
        raise FileNotFoundError(f"no .eml files found in {args.emails_dir}")
    return choices if args.all_fixtures else [random.Random(args.seed).choice(choices)]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sources = _choose_emails(args)
        tools = QuoteTools(args.database, args.reference_dir)
        client = OpenRouterClient(load_api_key(args.key_file))
        classifier = RequestClassifier(client, model=args.classifier_model)
        handler = QuoteHandler(client, tools, model=args.handler_model)
        pipeline = QuotePipeline(classifier, handler, tools)
        exit_code = 0
        for source_path in sources:
            run = pipeline.run(source_path)

            # Keep each run immutable and self-contained so debugging one model
            # call never overwrites evidence from a previous attempt.
            run_dir = args.output_dir.resolve() / run.run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            json_path = run_dir / "run.json"
            markdown_path = run_dir / "proposal.md"
            json_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
            markdown_path.write_text(render_markdown(run), encoding="utf-8")

            print(f"Email: {source_path}", flush=True)
            print(f"Outcome: {run.result.outcome.value}", flush=True)
            print(f"Proposal: {markdown_path}", flush=True)
            print(f"Audit record: {json_path}", flush=True)
            if run.result.outcome == Outcome.PROCESSING_ERROR:
                exit_code = 1
        return exit_code
    except Exception as exc:
        print(f"quote-agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
