"""Parse ``##### YEAR. VENUE. TITLE`` headings from Markdown notes and HTML documents."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

from .models import HeadingEntry, Issue

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]*(?P<body>.*?)[ \t]*$")
_YEAR_RE = re.compile(
    r"^(?:[#hH\-\*•\s]*)(?P<year>(?:19|20)\d{2})\s*[.\u3002]\s*(?P<rest>.+)$", re.DOTALL
)
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


class _HeadingHTMLParser(HTMLParser):
    """HTML parser to extract text inside <hN> tags."""

    def __init__(self, target_level: int = 5) -> None:
        super().__init__(convert_charrefs=True)
        self.target_tag = f"h{target_level}"
        self.in_target = False
        self.current_chunks: list[str] = []
        self.current_line = 0
        self.raw_headings: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == self.target_tag:
            self.in_target = True
            self.current_chunks = []
            self.current_line = self.getpos()[0]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == self.target_tag and self.in_target:
            self.in_target = False
            text = "".join(self.current_chunks)
            self.raw_headings.append((self.current_line, text))
            self.current_chunks = []

    def handle_data(self, data: str) -> None:
        if self.in_target:
            self.current_chunks.append(data)


def parse_html(
    text: str,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Extract all usable headings of the requested level from an HTML document.

    Only headings of exactly ``<h{heading_level}>`` are considered. Empty headings
    and headings that do not contain a year/venue/title are reported as issues
    and skipped, matching the behavior of ``parse_markdown``.
    """

    if not 1 <= heading_level <= 6:
        raise ValueError("heading_level must be between 1 and 6")

    parser = _HeadingHTMLParser(target_level=heading_level)
    parser.feed(text)
    parser.close()

    entries: list[HeadingEntry] = []
    issues: list[Issue] = []
    next_index = 1

    for line_no, raw in parser.raw_headings:
        cleaned = clean_markdown_text(raw)
        if not cleaned:
            # Empty separator heading
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
                raw_heading=cleaned,
                year=year,
                venue=venue,
                title=title,
            )
        )
        next_index += 1

    return entries, issues


def resolve_document_path(path: str | Path) -> Path:
    """Resolve a path to an existing Markdown or HTML file.

    If given a directory like ``usr/RVVAE``, looks for ``RVVAE.html``,
    ``index.html``, or any other HTML / Markdown document inside the directory.
    """

    p = Path(path).expanduser()
    if p.is_dir():
        # 1. Look for same-name html: e.g. usr/RVVAE -> usr/RVVAE/RVVAE.html
        cand = p / f"{p.name}.html"
        if cand.is_file():
            return cand
        # 2. Look for index.html
        cand = p / "index.html"
        if cand.is_file():
            return cand
        # 3. Look for any html or htm file
        html_files = sorted(list(p.glob("*.html")) + list(p.glob("*.htm")))
        if html_files:
            return html_files[0]
        # 4. Look for same-name md
        cand = p / f"{p.name}.md"
        if cand.is_file():
            return cand
        # 5. Look for any markdown file
        md_files = sorted(list(p.glob("*.md")) + list(p.glob("*.markdown")))
        if md_files:
            return md_files[0]
        return p

    if p.is_file():
        return p

    # If the exact path does not exist, try common extensions
    for ext in (".html", ".htm", ".md", ".markdown"):
        cand = p.with_suffix(ext)
        if cand.is_file():
            return cand

    return p


def is_html_document(path: str | Path, content: str | None = None) -> bool:
    """Whether a file or content string represents an HTML document."""

    p = Path(path)
    if p.suffix.lower() in {".html", ".htm", ".xhtml"}:
        return True
    if content is not None:
        sample = content[:2048].lower()
        if "<!doctype html" in sample or "<html" in sample:
            return True
        if re.search(r"<(?:head|body|h[1-6]|span|div)\b", sample):
            return True
    return False


def parse_html_file(
    path: str | Path,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Read an HTML file and extract its bibliographic headings."""

    resolved = resolve_document_path(path)
    text = resolved.read_text(encoding="utf-8-sig", errors="replace")
    return parse_html(text, heading_level=heading_level)


def parse_document_file(
    path: str | Path,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Read a Markdown or HTML file and extract its bibliographic headings.

    Supports direct .md/.html files as well as directories (e.g. ``usr/RVVAE``).
    """

    resolved_path = resolve_document_path(path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"找不到文献文档：{path}")

    text = resolved_path.read_text(encoding="utf-8-sig", errors="replace")
    if is_html_document(resolved_path, text):
        return parse_html(text, heading_level=heading_level)
    return parse_markdown(text, heading_level=heading_level)


def parse_markdown_file(
    path: str | Path,
    *,
    heading_level: int = 5,
) -> tuple[list[HeadingEntry], list[Issue]]:
    """Read a Markdown or HTML file and extract its bibliographic headings."""

    return parse_document_file(path, heading_level=heading_level)
