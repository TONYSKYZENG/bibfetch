"""Parse ``##### YEAR. VENUE. TITLE`` headings from Markdown notes."""

from __future__ import annotations

import html
import re
from pathlib import Path

from .models import HeadingEntry, Issue

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]*(?P<body>.*?)[ \t]*$")
_YEAR_RE = re.compile(r"^(?P<year>(?:19|20)\d{2})\s*[.\u3002]\s*(?P<rest>.+)$", re.DOTALL)
# The venue is the shortest segment ending in a period followed by whitespace.
_VENUE_TITLE_RE = re.compile(r"^(?P<venue>.+?)[.\u3002]\s+(?P<title>.+)$", re.DOTALL)
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>&$])")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_UNDERSCORE_OPEN_RE = re.compile(r"(?<![\w\\])_{1,3}(?=\S)")
_MD_UNDERSCORE_CLOSE_RE = re.compile(r"(?<=\S)_{1,3}(?![\w\\])")


def _strip_inline_markup(value: str) -> str:
    """Remove common emphasis markers without touching intra-word underscores."""

    value = re.sub(r"(?<!\\)[*`]+", "", value)
    value = re.sub(r"(?<!\\)~{2}", "", value)
    value = _MD_UNDERSCORE_OPEN_RE.sub("", value)
    value = _MD_UNDERSCORE_CLOSE_RE.sub("", value)
    return value



def clean_markdown_text(value: str) -> str:
    """Turn a Markdown heading fragment into searchable plain text."""

    value = html.unescape(value)
    value = _MD_LINK_RE.sub(r"\1", value)
    value = _HTML_TAG_RE.sub("", value)
    value = _strip_inline_markup(value)
    value = _MD_ESCAPE_RE.sub(r"\1", value)
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_heading_body(body: str) -> tuple[int, str, str] | None:
    """Parse ``2026. ACL Findings. Some title`` into ``(year, venue, title)``.

    Returns ``None`` when the heading does not follow the expected shape.
    """

    text = clean_markdown_text(body)
    if not text:
        return None

    year_match = _YEAR_RE.match(text)
    if not year_match:
        return None

    year = int(year_match.group("year"))
    rest = year_match.group("rest").strip()
    venue_match = _VENUE_TITLE_RE.match(rest)
    if not venue_match:
        return None

    venue = venue_match.group("venue").strip(" .\u3002")
    title = venue_match.group("title").strip(" .\u3002")
    if not venue or not title:
        return None
    return year, venue, title


def parse_markdown(
    text: str,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Extract all usable headings of the requested level.

    Only headings of exactly ``heading_level`` are considered. Empty headings
    and headings that do not contain a year/venue/title are reported as issues
    and skipped.
    """

    if not 1 <= heading_level <= 6:
        raise ValueError("heading_level must be between 1 and 6")

    entries: list[HeadingEntry] = []
    issues: list[Issue] = []
    next_index = 1

    for line_no, line in enumerate(text.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if not match or len(match.group("hashes")) != heading_level:
            continue

        raw = match.group("body")
        if not clean_markdown_text(raw):
            # The source document intentionally contains empty separator headings.
            continue

        parsed = parse_heading_body(raw)
        if parsed is None:
            issues.append(
                Issue(
                    code="heading_parse_failed",
                    message=f"line {line_no}: could not parse heading: {raw!r}",
                )
            )
            continue

        year, venue, title = parsed
        entries.append(
            HeadingEntry(
                index=next_index,
                line_no=line_no,
                raw_heading=raw,
                year=year,
                venue=venue,
                title=title,
            )
        )
        next_index += 1

    return entries, issues


def parse_markdown_file(
    path: str | Path,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Read a Markdown file and extract its bibliographic headings."""

    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8-sig")
    return parse_markdown(text, heading_level=heading_level)
