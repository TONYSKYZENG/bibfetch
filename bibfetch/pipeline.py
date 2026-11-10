"""End-to-end lookup, resolution, and merge pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bibtex import (
    construct_bibtex,
    extract_first_author,
    first_author_from_work,
    make_citation_key,
    merge_entries,
    normalize_crossref_bibtex,
)
from .matching import (
    choose_best,
    normalize_title,
    rank_candidates,
    venue_family,
)
from .md_parser import parse_markdown_file
from .models import Candidate, HeadingEntry, Issue, ResolvedEntry
from .openalex import ApiError, OpenAlexClient
from .report import write_bibtex, write_json_report, write_missing_report

LOGGER = logging.getLogger(__name__)


@dataclass
class PipelineOptions:
    """Tunable behaviour for one run."""

    heading_level: int = 5
    min_similarity: float = 0.90
    min_score: float = 5.0
    report: bool = True


@dataclass
class PipelineResult:
    input_path: Path
    output_path: Path
    report_path: Path | None
    missing_path: Path
    total_headings: int
    entries: list[ResolvedEntry]
    global_issues: list[Issue]

    @property
    def found(self) -> int:
        return sum(1 for entry in self.entries if entry.found)

    @property
    def missing(self) -> int:
        return len(self.entries) - self.found

    @property
    def warnings(self) -> int:
        return sum(1 for entry in self.entries for issue in entry.issues if issue.severity == "warning")


def _dedupe_works(works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for work in works:
        key = str(work.get("id") or "")
        if not key:
            key = "|".join(
                (
                    str(work.get("doi") or ""),
                    str(work.get("title") or ""),
                    str(work.get("publication_year") or ""),
                )
            )
        if key in seen:
            continue
        seen.add(key)
        unique.append(work)
    return unique


def _lookup(
    client: OpenAlexClient,
    entry: HeadingEntry,
    *,
    min_similarity: float,
    min_score: float,
) -> tuple[Candidate | None, bool]:
    """Find the best OpenAlex candidate for one heading.

    Returns ``(candidate, used_broad_search)``.  The broad title search is a
    fallback for records where OpenAlex title phrase-search fails (for example
    when a publisher strips the ``Position:`` prefix).
    """

    works = client.search_title(entry.title)
    candidates = rank_candidates(works, entry, min_similarity=min_similarity)
    best = choose_best(candidates, min_score=min_score)

    family = venue_family(entry.venue)
    needs_broad_search = best is None or (
        not best.is_formal and family not in {None, "arxiv"}
    )
    if needs_broad_search:
        LOGGER.debug("using broad OpenAlex search for: %s", entry.title)
        broad_works = client.search_broad(entry.title)
        merged = _dedupe_works(works + broad_works)
        candidates = rank_candidates(merged, entry, min_similarity=min_similarity)
        best = choose_best(candidates, min_score=min_score)
        return best, True

    return best, False


def _resolve_bibtex(
    client: OpenAlexClient,
    candidate: Candidate,
    entry: HeadingEntry,
    key: str,
) -> tuple[str, str, list[Issue]]:
    """Return ``(bibtex, source, issues)`` using DOI first, OpenAlex second."""

    issues: list[Issue] = []
    if candidate.doi:
        try:
            raw = client.fetch_bibtex(candidate.doi)
            return normalize_crossref_bibtex(raw, key), "doi-content-negotiation", issues
        except (ApiError, ValueError) as exc:
            issues.append(
                Issue(
                    code="bibtex_fetch_failed",
                    message=f"DOI {candidate.doi} could not be resolved to BibTeX; built from OpenAlex metadata ({exc})",
                )
            )

    return construct_bibtex(candidate.work, candidate, entry, key), "openalex-metadata", issues


def _entry_issues(candidate: Candidate, entry: HeadingEntry) -> list[Issue]:
    issues: list[Issue] = []
    family = venue_family(entry.venue)

    if candidate.title_similarity < 0.97:
        issues.append(
            Issue(
                code="fuzzy_title_match",
                message=f"title match is fuzzy ({candidate.title_similarity:.2f}): {candidate.work.get('title')!r}",
            )
        )

    if candidate.year is not None:
        difference = abs(candidate.year - entry.year)
        if difference > 1:
            issues.append(
                Issue(
                    code="year_mismatch",
                    message=f"heading year {entry.year} differs from OpenAlex year {candidate.year}",
                )
            )

    if family is not None and family != "arxiv" and not candidate.is_preprint and not candidate.venue_match:
        issues.append(
            Issue(
                code="venue_mismatch",
                message=(
                    f"heading venue {entry.venue!r} does not match OpenAlex source "
                    f"{candidate.source_name!r}"
                ),
            )
        )

    if candidate.is_preprint and family not in {None, "arxiv"}:
        issues.append(
            Issue(
                code="preprint_only",
                message=(
                    f"only a preprint/repository record was found ({candidate.source_name or candidate.source_type}); "
                    f"expected {entry.venue}"
                ),
            )
        )

    if not candidate.doi:
        issues.append(
            Issue(
                code="missing_doi",
                message="no DOI in OpenAlex; BibTeX was constructed from OpenAlex metadata",
            )
        )

    return issues


def resolve_entry(
    client: OpenAlexClient,
    entry: HeadingEntry,
    *,
    min_similarity: float = 0.90,
    min_score: float = 5.0,
    used_keys: set[str] | None = None,
) -> ResolvedEntry:
    """Look up one heading and turn it into a citable :class:`ResolvedEntry`.

    This is the single-entry version of :func:`run_pipeline`, shared with the
    GUI.  It performs the same search, scoring, citation-key, and BibTeX
    resolution steps, but leaves output writing to the caller.
    """

    used_keys = used_keys if used_keys is not None else set()
    candidate, _ = _lookup(
        client,
        entry,
        min_similarity=min_similarity,
        min_score=min_score,
    )
    if candidate is None:
        return ResolvedEntry(
            entry=entry,
            candidate=None,
            bibtex=None,
            citation_key=None,
            bibtex_source=None,
            issues=[
                Issue(
                    code="not_found",
                    message="no OpenAlex record met the title-similarity and confidence thresholds",
                    severity="error",
                )
            ],
        )

    issues = _entry_issues(candidate, entry)
    first_author = first_author_from_work(candidate.work)
    key = make_citation_key(
        first_author,
        candidate.year or entry.year,
        entry.title,
        used_keys,
    )
    bibtex, bibtex_source, fetch_issues = _resolve_bibtex(client, candidate, entry, key)
    issues.extend(fetch_issues)
    return ResolvedEntry(
        entry=entry,
        candidate=candidate,
        bibtex=bibtex,
        citation_key=key,
        bibtex_source=bibtex_source,
        issues=issues,
    )


def run_pipeline(
    input_path: str | Path,
    output_path: str | Path,
    client: OpenAlexClient,
    *,
    options: PipelineOptions | None = None,
    report_path: str | Path | None = None,
    missing_path: str | Path | None = None,
) -> PipelineResult:
    """Read Markdown headings, query OpenAlex, and merge the resolved BibTeX."""

    options = options or PipelineOptions()
    input_path = Path(input_path)
    output_path = Path(output_path)
    if report_path is None:
        report_path = output_path.with_suffix(".report.json")
    else:
        report_path = Path(report_path)
    if missing_path is None:
        missing_path = output_path.with_suffix(".missing.txt")
    else:
        missing_path = Path(missing_path)

    headings, global_issues = parse_markdown_file(input_path, heading_level=options.heading_level)
    if not headings:
        global_issues.append(Issue(code="no_headings", message="no usable headings found", severity="error"))

    # Repeated headings would produce duplicate keys; keep the first one.
    entries: list[HeadingEntry] = []
    seen_titles: set[str] = set()
    for heading in headings:
        normalized = normalize_title(heading.title)
        if normalized in seen_titles:
            global_issues.append(
                Issue(
                    code="duplicate_heading",
                    message=f"line {heading.line_no}: duplicate title skipped: {heading.title}",
                )
            )
            continue
        seen_titles.add(normalized)
        entries.append(heading)

    resolved_entries: list[ResolvedEntry] = []
    used_keys: set[str] = set()

    for position, entry in enumerate(entries, start=1):
        LOGGER.info("[%d/%d] searching: %s", position, len(entries), entry.title)
        try:
            resolved = resolve_entry(
                client,
                entry,
                min_similarity=options.min_similarity,
                min_score=options.min_score,
                used_keys=used_keys,
            )
        except ApiError as exc:
            issue = Issue(
                code="openalex_request_failed",
                message=f"OpenAlex request failed: {exc}",
                severity="error",
            )
            resolved_entries.append(ResolvedEntry(entry, None, None, None, None, [issue]))
            LOGGER.error("[%d/%d] request failed: %s", position, len(entries), entry.title)
            continue

        if resolved.candidate is None:
            resolved_entries.append(resolved)
            LOGGER.warning("[%d/%d] NOT FOUND: %s", position, len(entries), entry.title)
            continue

        if resolved.candidate is not None and resolved.bibtex is not None:
            if first_author_from_work(resolved.candidate.work) is None:
                parsed_author = extract_first_author(resolved.bibtex)
                if parsed_author:
                    # The key was already generated; only enrich the warning path.
                    LOGGER.debug("first author recovered from BibTeX: %s", parsed_author)

        for issue in resolved.issues:
            LOGGER.warning("[%d/%d] %s: %s", position, len(entries), issue.code, issue.message)
        LOGGER.info(
            "[%d/%d] OK: %s (%s, %s)",
            position,
            len(entries),
            resolved.citation_key,
            resolved.bibtex_source,
            resolved.candidate.doi or "no DOI",
        )

        resolved_entries.append(resolved)

    body = merge_entries(resolved.bibtex or "" for resolved in resolved_entries)
    write_bibtex(
        output_path,
        body,
        input_path=input_path,
        total=len(resolved_entries),
        found=sum(1 for resolved in resolved_entries if resolved.found),
    )
    if options.report:
        write_json_report(
            report_path,
            input_path=input_path,
            output_path=output_path,
            resolved_entries=resolved_entries,
            global_issues=global_issues,
            total_headings=len(headings),
        )
    missing_count = write_missing_report(missing_path, resolved_entries)

    return PipelineResult(
        input_path=input_path,
        output_path=output_path,
        report_path=report_path if options.report else None,
        missing_path=missing_path,
        total_headings=len(headings),
        entries=resolved_entries,
        global_issues=global_issues,
    )
