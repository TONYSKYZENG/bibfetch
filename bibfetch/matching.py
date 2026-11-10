"""Score OpenAlex works and choose the most citable record.

The ranking deliberately prefers publisher records (journal articles and
conference proceedings) over arXiv/repository records.  A preprint is still
accepted when it is the only record available.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from .md_parser import clean_markdown_text
from .models import Candidate, HeadingEntry

# Phrases that identify a venue in OpenAlex source names.  The keys are
# normalised heading venues; values are needles searched in source names.
_VENUE_ALIASES: dict[str, tuple[str, ...]] = {
    "acl_findings": (
        "findings of the association for computational linguistics",
        "findings of acl",
    ),
    "acl": (
        "association for computational linguistics",
        "annual meeting of the association for computational linguistics",
    ),
    "emnlp": (
        "empirical methods in natural language processing",
        "conference on empirical methods in natural language processing",
    ),
    "naacl": (
        "north american chapter of the association for computational linguistics",
    ),
    "eacl": ("european chapter of the association for computational linguistics",),
    "coling": ("international conference on computational linguistics",),
    "iclr": ("international conference on learning representations",),
    "neurips": (
        "neural information processing systems",
        "advances in neural information processing systems",
    ),
    "icml": ("international conference on machine learning",),
    "aaai": ("aaai conference on artificial intelligence",),
    "ijcai": ("international joint conference on artificial intelligence",),
    "tmlr": ("transactions on machine learning research",),
    "jmlr": ("journal of machine learning research",),
    "tois": ("acm transactions on information systems",),
    "tkde": ("ieee transactions on knowledge and data engineering",),
    "tpami": ("ieee transactions on pattern analysis and machine intelligence",),
    "arxiv": ("arxiv",),
}

_FORMAL_SOURCE_TYPES = {"journal", "conference", "book", "book-series"}
_POSITION_PREFIX_RE = re.compile(r"^\s*position\s*[:\-]\s*", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _ascii_fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char))


def normalize_title(value: str | None) -> str:
    """Create a comparison-friendly title (case, punctuation, escapes removed)."""

    if not value:
        return ""
    text = clean_markdown_text(str(value))
    text = _ascii_fold(text).casefold()
    text = _POSITION_PREFIX_RE.sub("", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return _WS_RE.sub(" ", text).strip()


def title_similarity(left: str | None, right: str | None) -> float:
    """Return a 0..1 similarity combining sequence and token overlap."""

    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    from difflib import SequenceMatcher

    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    union = a_tokens | b_tokens
    jaccard = len(a_tokens & b_tokens) / len(union) if union else 0.0
    # Jaccard is more forgiving of small rewording and word-order changes.
    return max(sequence, jaccard)


def venue_family(venue: str | None) -> str | None:
    """Map a free-form venue label to a known venue family."""

    if not venue:
        return None
    value = _ascii_fold(clean_markdown_text(venue)).casefold()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()

    compact = value.replace(" ", "")
    if "findings" in value and ("acl" in value or "associationforcomputationallinguistics" in compact):
        return "acl_findings"
    if value.startswith("arxiv") or "arxiv" in compact:
        return "arxiv"

    compact_venues = {
        "acl": "acl",
        "emnlp": "emnlp",
        "naacl": "naacl",
        "eacl": "eacl",
        "coling": "coling",
        "iclr": "iclr",
        "neurips": "neurips",
        "nips": "neurips",
        "icml": "icml",
        "aaai": "aaai",
        "ijcai": "ijcai",
        "tmlr": "tmlr",
        "jmlr": "jmlr",
        "tois": "tois",
        "tkde": "tkde",
        "tpami": "tpami",
    }
    for token, family in compact_venues.items():
        if value == token or value.startswith(token + " ") or f" {token} " in f" {value} ":
            return family
    return None


def venue_matches(heading_venue: str | None, source_name: str | None) -> bool:
    """Return whether an OpenAlex source name matches the heading venue."""

    family = venue_family(heading_venue)
    if family is None or not source_name:
        return False
    source = _ascii_fold(source_name).casefold()
    source = re.sub(r"[^a-z0-9]+", " ", source)
    return any(needle in source for needle in _VENUE_ALIASES[family])


def _source(location: dict[str, Any] | None) -> dict[str, Any]:
    if not location:
        return {}
    return location.get("source") or {}


def _source_name(location: dict[str, Any] | None) -> str:
    source = _source(location)
    return str(source.get("display_name") or (location or {}).get("raw_source_name") or "")


def _source_type(location: dict[str, Any] | None) -> str:
    return str(_source(location).get("type") or "")


def _is_formal(location: dict[str, Any] | None, work_type: str) -> bool:
    source_type = _source_type(location)
    if source_type in _FORMAL_SOURCE_TYPES:
        return True
    if source_type in {"repository", "preprint"}:
        return False
    return work_type in {"article", "review", "conference-paper", "book-chapter"}


def _is_preprint(location: dict[str, Any] | None, work_type: str) -> bool:
    source_type = _source_type(location)
    name = _source_name(location).casefold()
    return (
        work_type in {"preprint", "other"}
        or source_type in {"repository", "preprint"}
        or "arxiv" in name
        or "ssrn" in name
    )


def _location_score(
    location: dict[str, Any],
    heading_venue: str,
    *,
    is_primary: bool,
) -> float:
    source_type = _source_type(location)
    work_type = "preprint" if source_type == "repository" else ""
    formal = _is_formal(location, work_type)
    name = _source_name(location)
    version = str(location.get("version") or "")

    score = 0.0
    if formal:
        score += 6.0
    if source_type in {"repository", "preprint"}:
        score -= 4.0
    if version == "publishedVersion":
        score += 4.0
    elif version == "acceptedVersion":
        score += 2.0
    elif version == "submittedVersion":
        score -= 1.0
    if location.get("is_published"):
        score += 1.0
    if location.get("doi"):
        score += 1.0
    if venue_matches(heading_venue, name):
        score += 3.0
    if "arxiv" in name.casefold():
        score -= 2.0
    if location.get("landing_page_url"):
        score += 0.2
    if is_primary:
        score += 0.1
    return score


def select_location(work: dict[str, Any], heading_venue: str) -> dict[str, Any] | None:
    """Choose the most formal location attached to an OpenAlex work."""

    primary = work.get("primary_location") or None
    locations: list[dict[str, Any]] = []
    if primary:
        locations.append(primary)
    for location in work.get("locations") or []:
        if location and location not in locations:
            locations.append(location)

    if not locations:
        return None

    def rank(location: dict[str, Any]) -> float:
        return _location_score(
            location,
            heading_venue,
            is_primary=location is primary,
        )

    return max(locations, key=rank)


def score_candidate(work: dict[str, Any], entry: HeadingEntry) -> Candidate:
    """Build a scored :class:`Candidate` from a raw OpenAlex work."""

    location = select_location(work, entry.venue)
    work_type = str(work.get("type") or "")
    source_name = _source_name(location)
    source_type = _source_type(location)
    year_raw = work.get("publication_year")
    year = int(year_raw) if isinstance(year_raw, int) else None
    doi = str((location or {}).get("doi") or work.get("doi") or "").strip() or None
    if doi:
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix) :]
                break

    matched_title = str(work.get("title") or work.get("display_name") or "")
    similarity = title_similarity(entry.title, matched_title)
    formal = _is_formal(location, work_type)
    preprint = _is_preprint(location, work_type)
    family = venue_family(entry.venue)
    matches_venue = venue_matches(entry.venue, source_name)

    score = 5.0 * similarity
    if similarity >= 0.999:
        score += 2.0

    if year is not None:
        difference = abs(year - entry.year)
        if difference == 0:
            score += 2.0
        elif difference == 1:
            score += 0.75
        else:
            score -= min(difference, 4) * 1.0

    if formal:
        score += 2.5
    elif preprint:
        score -= 2.0
    elif work_type == "other":
        score -= 2.5

    if doi:
        score += 0.5
    if matches_venue:
        score += 1.5
    elif family is not None:
        score -= 0.75

    if work.get("authorships"):
        score += 0.3

    return Candidate(
        work=work,
        score=score,
        title_similarity=similarity,
        location=location,
        source_name=source_name,
        source_type=source_type,
        work_type=work_type,
        doi=doi,
        year=year,
        is_formal=formal,
        is_preprint=preprint,
        venue_match=matches_venue,
    )


def rank_candidates(
    works: Iterable[dict[str, Any]],
    entry: HeadingEntry,
    *,
    min_similarity: float = 0.90,
) -> list[Candidate]:
    """Score works, drop weak title matches, and sort best-first."""

    candidates = [score_candidate(work, entry) for work in works]
    candidates = [candidate for candidate in candidates if candidate.title_similarity >= min_similarity]
    candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.title_similarity,
            candidate.is_formal,
        ),
        reverse=True,
    )
    return candidates


def choose_best(
    candidates: Iterable[Candidate],
    *,
    min_score: float = 5.0,
) -> Candidate | None:
    """Return the highest-scoring candidate above the confidence floor."""

    ranked = sorted(candidates, key=lambda candidate: (candidate.score, candidate.title_similarity), reverse=True)
    for candidate in ranked:
        if candidate.score >= min_score:
            return candidate
    return None
