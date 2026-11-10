"""Command-line interface for bibfetch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .cache import JsonCache
from .md_parser import resolve_document_path
from .openalex import ApiError, OpenAlexClient
from .pipeline import PipelineOptions, run_pipeline

LOGGER = logging.getLogger("bibfetch")


def _default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "bibfetch" / "openalex.json"
    return Path.home() / ".cache" / "bibfetch" / "openalex.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bibfetch",
        description=(
            "Read '##### YEAR. VENUE. TITLE' Markdown headings, look each title up "
            "in OpenAlex, and merge high-fidelity BibTeX entries into one file."
        ),
    )
    parser.add_argument("markdown", type=Path, help="Markdown or HTML file (or directory) to scan")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="merged .bib output path (default: alongside the Markdown file)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="JSON report path (default: <output>.report.json)",
    )
    parser.add_argument(
        "--missing",
        type=Path,
        default=None,
        help="missing-entry report path (default: <output>.missing.txt)",
    )
    parser.add_argument(
        "--heading-level",
        type=int,
        default=5,
        choices=range(1, 7),
        metavar="{1..6}",
        help="Markdown heading level to scan (default: 5)",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.90,
        help="minimum normalized title similarity (default: 0.90)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=5.0,
        help="minimum candidate confidence score (default: 5.0)",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=25,
        help="OpenAlex results to inspect per query (default: 25)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENALEX_API_KEY"),
        help="OpenAlex API key (default: OPENALEX_API_KEY)",
    )
    parser.add_argument(
        "--mailto",
        default=os.environ.get("OPENALEX_MAILTO"),
        help="email for the OpenAlex polite pool (default: OPENALEX_MAILTO)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help=f"cache path (default: {_default_cache_path()})",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=30.0,
        help="cache lifetime in days; 0 keeps entries forever (default: 30)",
    )
    parser.add_argument("--no-cache", action="store_true", help="disable the disk cache")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="retries for transient HTTP failures (default: 4)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="do not write the JSON report",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with status 2 when at least one heading remains unresolved",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="print debug details")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    input_path: Path = resolve_document_path(args.markdown)
    if not input_path.is_file():
        LOGGER.error("Document file not found: %s", args.markdown)
        return 1

    if args.output is not None:
        output_path = args.output
    elif args.markdown.is_dir():
        output_path = args.markdown.with_suffix(".bib")
    else:
        output_path = input_path.with_suffix(".bib")
    cache_path = None if args.no_cache else (args.cache or _default_cache_path())
    cache = JsonCache(cache_path, ttl_days=args.cache_ttl, enabled=not args.no_cache)
    client = OpenAlexClient(
        api_key=args.api_key,
        mailto=args.mailto,
        timeout=args.timeout,
        max_retries=max(0, args.max_retries),
        cache=cache,
        per_page=args.per_page,
    )

    try:
        result = run_pipeline(
            input_path,
            output_path,
            client,
            options=PipelineOptions(
                heading_level=args.heading_level,
                min_similarity=args.min_similarity,
                min_score=args.min_score,
                report=not args.no_report,
            ),
            report_path=args.report,
            missing_path=args.missing,
        )
    except (OSError, ValueError, ApiError) as exc:
        LOGGER.error("%s", exc)
        return 1
    finally:
        cache.flush()

    for issue in result.global_issues:
        LOGGER.warning("%s", issue.message)

    print(
        f"Parsed {result.total_headings} headings; resolved "
        f"{result.found}/{len(result.entries)} entries."
    )
    print(f"BibTeX:  {result.output_path}")
    if result.report_path is not None:
        print(f"Report:  {result.report_path}")
    if result.missing:
        print(f"Missing: {result.missing_path} ({result.missing})")

    if result.total_headings == 0:
        return 1
    if args.strict and result.missing:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
