"""Shared data models used by the bibliography pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HeadingEntry:
    """One bibliographic item parsed from a Markdown heading."""

    index: int
    line_no: int
    raw_heading: str
    year: int
    venue: str
    title: str


@dataclass
class Candidate:
    """A scored OpenAlex work plus the location selected for citation."""

    work: dict[str, Any]
    score: float
    title_similarity: float
    location: dict[str, Any] | None
    source_name: str
    source_type: str
    work_type: str
    doi: str | None
    year: int | None
    is_formal: bool
    is_preprint: bool
    venue_match: bool

    @property
    def openalex_id(self) -> str:
        return str(self.work.get("id") or "")


@dataclass(frozen=True)
class Issue:
    """A warning or error associated with a single entry."""

    code: str
    message: str
    severity: str = "warning"


@dataclass
class ResolvedEntry:
    """An entry after lookup, BibTeX resolution, and citation-key assignment."""

    entry: HeadingEntry
    candidate: Candidate | None
    bibtex: str | None
    citation_key: str | None
    bibtex_source: str | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.bibtex is not None
