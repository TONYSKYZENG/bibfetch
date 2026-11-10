"""Project persistence and lookup helpers for the bibfetch GUI.

The GUI keeps its own ``.bibgui.json`` project file.  It is deliberately
separate from the ``.report.json`` written by the command line pipeline: the
project file records the editable state (manual overrides, selected options,
resolved entries) so a session can be closed and resumed later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bibfetch.bibtex import merge_entries
from bibfetch.cache import JsonCache
from bibfetch.matching import normalize_title
from bibfetch.md_parser import parse_markdown_file
from bibfetch.models import Candidate, HeadingEntry, Issue, ResolvedEntry
from bibfetch.openalex import ApiError, OpenAlexClient
from bibfetch.pipeline import resolve_entry
from bibfetch.report import write_bibtex

PROJECT_VERSION = 1
PROJECT_SUFFIX = ".bibgui.json"

STATUS_PENDING = "pending"
STATUS_FOUND = "found"
STATUS_MISSING = "missing"
STATUS_MANUAL = "manual"

_BIBTEX_HEADER_RE = re.compile(r"@\s*(?P<type>[A-Za-z]+)\s*\{\s*(?P<key>[^,\s{}]+)\s*,")
_BIBTEX_TITLE_RE = re.compile(
    r"(?is)\btitle\s*=\s*(?:\{(?P<braced>(?:[^{}]|\{[^{}]*\})*)\}|"
    r'"(?P<quoted>(?:[^"\\]|\\.)*)")'
)
_BIBTEX_YEAR_RE = re.compile(r"(?i)\byear\s*=\s*[{\"]?(?P<value>(?:19|20)\d{2})")
_BIBTEX_VENUE_RE = re.compile(
    r"(?is)\b(?:booktitle|journal)\s*=\s*(?:\{(?P<braced>(?:[^{}]|\{[^{}]*\})*)\}|"
    r'"(?P<quoted>(?:[^"\\]|\\.)*)")'
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_cache_path() -> Path:
    """Same default cache location as the command line interface."""

    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "bibfetch" / "openalex.json"
    return Path.home() / ".cache" / "bibfetch" / "openalex.json"


def _entry_id(title: str) -> str:
    normalized = normalize_title(title) or title.strip().casefold()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def extract_bibtex_key(bibtex: str) -> str | None:
    """Return the citation key of the first BibTeX entry, if any."""

    match = _BIBTEX_HEADER_RE.search(bibtex or "")
    return match.group("key") if match else None


def parse_bibtex_entry(bibtex: str) -> dict[str, Any]:
    """Extract key, title, year, venue from a BibTeX entry."""
    title, year = parse_bibtex_identity(bibtex)
    key = extract_bibtex_key(bibtex)
    venue = None
    venue_match = _BIBTEX_VENUE_RE.search(bibtex or "")
    if venue_match:
        value = venue_match.group("braced")
        if value is None:
            value = venue_match.group("quoted") or ""
        venue = re.sub(r"\s+", " ", value).strip()
        venue = venue or None
    return {
        "key": key,
        "title": title,
        "year": year,
        "venue": venue,
    }


def parse_bibtex_identity(bibtex: str) -> tuple[str | None, int | None]:
    """Best-effort title/year extraction used for the list display."""

    title_match = _BIBTEX_TITLE_RE.search(bibtex or "")
    title = None
    if title_match:
        value = title_match.group("braced")
        if value is None:
            value = title_match.group("quoted") or ""
        title = re.sub(r"\s+", " ", value).strip()
        title = title or None
    year_match = _BIBTEX_YEAR_RE.search(bibtex or "")
    year = int(year_match.group("value")) if year_match else None
    return title, year


def looks_like_arxiv(*values: str | None) -> bool:
    """Whether a resolved record or BibTeX entry points at arXiv."""

    for value in values:
        if not value:
            continue
        lowered = value.casefold()
        if "arxiv" in lowered:
            return True
    return False


def _candidate_arxiv(candidate: Candidate, bibtex: str | None) -> bool:
    urls: list[str] = []
    for location in [candidate.location or {}] + list(candidate.work.get("locations") or []):
        if not isinstance(location, dict):
            continue
        for key in ("landing_page_url", "pdf_url"):
            if location.get(key):
                urls.append(str(location[key]))
    return looks_like_arxiv(
        candidate.source_name,
        candidate.source_type,
        candidate.doi,
        *urls,
        bibtex,
    )


def _candidate_is_arxiv_only(candidate: Candidate) -> bool:
    """Whether the selected record is an arXiv record without a formal venue.

    A formally published paper often also has an arXiv *location*; those must
    not be flagged.  The marker is only shown when OpenAlex actually selected a
    preprint/repository record.
    """

    source = f"{candidate.source_name} {candidate.source_type}".casefold()
    if not candidate.is_preprint and "arxiv" not in source:
        return False
    return _candidate_arxiv(candidate, None)


def _issue_dict(issue: Issue) -> dict[str, str]:
    return {"code": issue.code, "message": issue.message, "severity": issue.severity}


@dataclass
class ProjectOptions:
    """Settings persisted in the project file (except the API key)."""

    heading_level: int = 5
    min_similarity: float = 0.90
    min_score: float = 5.0
    per_page: int = 25
    mailto: str = ""
    # Kept in memory only: never written to the project JSON.
    api_key: str = ""
    use_cache: bool = True
    cache_ttl: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading_level": self.heading_level,
            "min_similarity": self.min_similarity,
            "min_score": self.min_score,
            "per_page": self.per_page,
            "mailto": self.mailto,
            "use_cache": self.use_cache,
            "cache_ttl": self.cache_ttl,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ProjectOptions":
        payload = payload or {}

        def number(key: str, default: float, cast):
            value = payload.get(key)
            if value is None:
                return default
            try:
                return cast(value)
            except (TypeError, ValueError):
                return default

        return cls(
            heading_level=int(number("heading_level", 5, int)),
            min_similarity=float(number("min_similarity", 0.90, float)),
            min_score=float(number("min_score", 5.0, float)),
            per_page=int(number("per_page", 25, int)),
            mailto=str(payload.get("mailto") or ""),
            use_cache=bool(payload.get("use_cache", True)),
            cache_ttl=float(number("cache_ttl", 30.0, float)),
        )


@dataclass
class EntryRecord:
    """Editable state for one Markdown heading."""

    id: str
    line: int
    raw_heading: str
    year: int
    venue: str
    title: str
    checked: bool = False
    matched_title: str | None = None
    matched_year: int | None = None
    manual_title: str | None = None
    manual_year: int | None = None
    citation_key: str | None = None
    doi: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    work_type: str | None = None
    is_formal: bool = False
    is_preprint: bool = False
    is_arxiv: bool = False
    bibtex_source: str | None = None
    bibtex: str | None = None
    manual_bibtex: str | None = None
    issues: list[dict[str, str]] = field(default_factory=list)
    is_manual_entry: bool = False

    # ---------------------------------------------------------------- display
    @property
    def status(self) -> str:
        if self.manual_bibtex:
            return STATUS_MANUAL
        if not self.checked:
            return STATUS_PENDING
        return STATUS_FOUND if self.bibtex else STATUS_MISSING

    @property
    def display_title(self) -> str:
        return self.manual_title or self.matched_title or self.title

    @property
    def display_year(self) -> int:
        if self.manual_year is not None:
            return self.manual_year
        if self.matched_year is not None:
            return self.matched_year
        return self.year

    @property
    def is_arxiv_only(self) -> bool:
        """Show a warning marker when the entry only has an arXiv record.

        Manual overrides never display the warning marker per requirement.
        """
        if self.manual_bibtex:
            return False
        return self.is_arxiv

    @property
    def effective_bibtex(self) -> str | None:
        return self.manual_bibtex or self.bibtex

    @property
    def effective_source(self) -> str | None:
        return "manual" if self.manual_bibtex else self.bibtex_source

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.get("severity") == "warning")

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.get("severity") == "error")

    def heading(self) -> HeadingEntry:
        return HeadingEntry(
            index=1,
            line_no=self.line,
            raw_heading=self.raw_heading,
            year=self.year,
            venue=self.venue,
            title=self.title,
        )

    # ------------------------------------------------------------------ state
    def apply_resolved(self, resolved: ResolvedEntry) -> None:
        self.checked = True
        self.issues = [_issue_dict(issue) for issue in resolved.issues]
        self.citation_key = resolved.citation_key
        self.bibtex = resolved.bibtex
        self.bibtex_source = resolved.bibtex_source

        candidate = resolved.candidate
        if candidate is None:
            self.matched_title = None
            self.matched_year = None
            self.doi = None
            self.source_name = None
            self.source_type = None
            self.work_type = None
            self.is_formal = False
            self.is_preprint = False
            self.is_arxiv = False
            return

        self.matched_title = str(candidate.work.get("title") or candidate.work.get("display_name") or "") or None
        self.matched_year = candidate.year
        self.doi = candidate.doi
        self.source_name = candidate.source_name or None
        self.source_type = candidate.source_type or None
        self.work_type = candidate.work_type or None
        self.is_formal = candidate.is_formal
        self.is_preprint = candidate.is_preprint
        self.is_arxiv = _candidate_is_arxiv_only(candidate)

    def apply_manual(self, bibtex: str) -> str:
        """Store a pasted BibTeX entry and return its citation key."""

        text = (bibtex or "").strip()
        if not text:
            raise ValueError("BibTeX 内容为空")
        key = extract_bibtex_key(text)
        if not key:
            raise ValueError("没有找到 BibTeX 引用键，内容应以 @type{key, 开头")
        self.manual_bibtex = text + "\n"
        self.citation_key = key
        title, year = parse_bibtex_identity(text)
        self.manual_title = title
        self.manual_year = year
        return key

    def clear_manual(self) -> None:
        self.manual_bibtex = None
        self.manual_title = None
        self.manual_year = None

    def mark_error(self, code: str, message: str) -> None:
        self.checked = True
        self.bibtex = None
        self.issues = [{"code": code, "message": message, "severity": "error"}]

    # ------------------------------------------------------------- serializer
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "line": self.line,
            "raw_heading": self.raw_heading,
            "year": self.year,
            "venue": self.venue,
            "title": self.title,
            "status": self.status,
            "checked": self.checked,
            "matched_title": self.matched_title,
            "matched_year": self.matched_year,
            "manual_title": self.manual_title,
            "manual_year": self.manual_year,
            "citation_key": self.citation_key,
            "doi": self.doi,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "work_type": self.work_type,
            "is_formal": self.is_formal,
            "is_preprint": self.is_preprint,
            "is_arxiv": self.is_arxiv,
            "bibtex_source": self.bibtex_source,
            "bibtex": self.bibtex,
            "manual_bibtex": self.manual_bibtex,
            "issues": self.issues,
            "is_manual_entry": self.is_manual_entry,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EntryRecord":
        title = str(payload.get("title") or "")
        return cls(
            id=str(payload.get("id") or _entry_id(title)),
            line=int(payload.get("line", 0) or 0),
            raw_heading=str(payload.get("raw_heading") or ""),
            year=int(payload.get("year", 0) or 0),
            venue=str(payload.get("venue") or ""),
            title=title,
            checked=bool(payload.get("checked", payload.get("status") not in {None, "pending"})),
            matched_title=payload.get("matched_title") or None,
            matched_year=payload.get("matched_year"),
            manual_title=payload.get("manual_title") or None,
            manual_year=payload.get("manual_year"),
            citation_key=payload.get("citation_key") or None,
            doi=payload.get("doi") or None,
            source_name=payload.get("source_name") or None,
            source_type=payload.get("source_type") or None,
            work_type=payload.get("work_type") or None,
            is_formal=bool(payload.get("is_formal", False)),
            is_preprint=bool(payload.get("is_preprint", False)),
            is_arxiv=bool(payload.get("is_arxiv", False)),
            bibtex_source=payload.get("bibtex_source") or None,
            bibtex=payload.get("bibtex") or None,
            manual_bibtex=payload.get("manual_bibtex") or None,
            issues=list(payload.get("issues") or []),
            is_manual_entry=bool(payload.get("is_manual_entry", False)),
        )


@dataclass
class Project:
    """A GUI project backed by one ``.bibgui.json`` file."""

    path: Path | None = None
    name: str = "未命名工程"
    markdown_path: Path | None = None
    output_path: Path | None = None
    options: ProjectOptions = field(default_factory=ProjectOptions)
    entries: list[EntryRecord] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def new(cls, path: str | Path) -> "Project":
        project_path = Path(path).expanduser()
        return cls(path=project_path, name=_project_name(project_path))

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        project_path = Path(path).expanduser()
        payload = json.loads(project_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("工程文件格式不正确")
        entries_payload = payload.get("entries") or []
        if not isinstance(entries_payload, list):
            raise ValueError("工程文件中的 entries 字段格式不正确")
        project = cls(
            path=project_path,
            name=str(payload.get("name") or _project_name(project_path)),
            markdown_path=_optional_path(payload.get("markdown")),
            output_path=_optional_path(payload.get("output")),
            options=ProjectOptions.from_dict(payload.get("options")),
            entries=[EntryRecord.from_dict(item) for item in entries_payload if isinstance(item, dict)],
            created_at=str(payload.get("created_at") or _now()),
            updated_at=str(payload.get("updated_at") or _now()),
        )
        return project

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path).expanduser() if path is not None else self.path
        if target is None:
            raise ValueError("还没有工程文件路径")
        self.path = target
        self.name = _project_name(target)
        self.updated_at = _now()
        payload = {
            "version": PROJECT_VERSION,
            "name": self.name,
            "markdown": str(self.markdown_path) if self.markdown_path else None,
            "output": str(self.output_path) if self.output_path else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "options": self.options.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    # --------------------------------------------------------------- import
    def import_markdown(
        self,
        markdown_path: str | Path,
        *,
        heading_level: int | None = None,
    ) -> tuple[int, list[Issue]]:
        """(Re)load headings while preserving overrides for unchanged titles.

        Returns ``(number_of_entries, issues)``.  Existing lookup results and
        manual overrides survive a re-import as long as the title is unchanged.
        """

        source = Path(markdown_path).expanduser()
        level = heading_level if heading_level is not None else self.options.heading_level
        headings, issues = parse_markdown_file(source, heading_level=level)

        existing = {normalize_title(entry.title): entry for entry in self.entries}
        seen: set[str] = set()
        records: list[EntryRecord] = []
        for heading in headings:
            normalized = normalize_title(heading.title)
            if normalized in seen:
                issues.append(
                    Issue(
                        code="duplicate_heading",
                        message=f"line {heading.line_no}: duplicate title skipped: {heading.title}",
                    )
                )
                continue
            seen.add(normalized)

            record = existing.get(normalized)
            if record is None:
                record = EntryRecord(
                    id=_entry_id(heading.title),
                    line=heading.line_no,
                    raw_heading=heading.raw_heading,
                    year=heading.year,
                    venue=heading.venue,
                    title=heading.title,
                )
            else:
                record.line = heading.line_no
                record.raw_heading = heading.raw_heading
                record.year = heading.year
                record.venue = heading.venue
                record.title = heading.title
            records.append(record)

        # Retain manually inserted entries or overrides not present in markdown
        for entry in self.entries:
            normalized = normalize_title(entry.title)
            if normalized not in seen and (entry.is_manual_entry or entry.manual_bibtex):
                records.append(entry)
                seen.add(normalized)

        self.entries = records
        self.markdown_path = source
        if self.output_path is None:
            self.output_path = source.with_suffix(".bib")
        return len(records), issues

    def sync_markdown(self) -> tuple[int, int, list[Issue]]:
        """Re-read the project's source markdown and synchronize entries.

        Returns ``(total_count, new_count, issues)``.
        """
        if self.markdown_path is None:
            raise ValueError("当前工程尚未关联源 Markdown 文件")
        prev_keys = {normalize_title(entry.title) for entry in self.entries}
        total, issues = self.import_markdown(self.markdown_path)
        new_count = sum(1 for entry in self.entries if normalize_title(entry.title) not in prev_keys)
        return total, new_count, issues

    def insert_entry(
        self,
        title: str,
        year: int = 0,
        venue: str = "",
        bibtex: str | None = None,
    ) -> EntryRecord:
        """Manually insert a literature entry."""
        title = title.strip()
        if not title:
            raise ValueError("文献标题不能为空")
        norm = normalize_title(title)
        for existing in self.entries:
            if normalize_title(existing.title) == norm:
                raise ValueError(f"文献标题已存在：{title}")

        record = EntryRecord(
            id=_entry_id(title),
            line=0,
            raw_heading=f"{year}. {venue}. {title}".strip(". "),
            year=year,
            venue=venue,
            title=title,
            is_manual_entry=True,
        )
        if bibtex and bibtex.strip():
            record.apply_manual(bibtex)

        self.entries.append(record)
        return record

    def remove_entry(self, entry_id: str) -> bool:
        """Remove an entry by ID. Returns True if found and removed."""
        for i, entry in enumerate(self.entries):
            if entry.id == entry_id:
                del self.entries[i]
                return True
        return False

    # --------------------------------------------------------------- lookup
    def lookup(
        self,
        record: EntryRecord,
        client: OpenAlexClient,
        options: ProjectOptions | None = None,
        *,
        used_keys: set[str] | None = None,
    ) -> EntryRecord:
        """Resolve one record against OpenAlex and store the result."""

        options = options or self.options
        try:
            resolved = resolve_entry(
                client,
                record.heading(),
                min_similarity=options.min_similarity,
                min_score=options.min_score,
                used_keys=used_keys,
            )
        except ApiError as exc:
            record.mark_error("openalex_request_failed", f"OpenAlex 请求失败：{exc}")
            return record
        record.apply_resolved(resolved)
        return record

    # -------------------------------------------------------------- generate
    def generate(self, output_path: str | Path | None = None) -> tuple[Path, int, int, list[str]]:
        """Write the merged bibliography.

        Returns ``(path, written, total, warnings)``.  Manual overrides win
        over the automatically fetched BibTeX.
        """

        target = Path(output_path).expanduser() if output_path is not None else self.output_path
        if target is None:
            if self.markdown_path is not None:
                target = self.markdown_path.with_suffix(".bib")
            else:
                raise ValueError("请先选择输出 .bib 路径")

        blocks: list[str] = []
        warnings: list[str] = []
        used_keys: dict[str, str] = {}

        # "all-in-one": every usable entry in one file, newest first.  Entries
        # without a known year go to the bottom, then title order keeps output
        # deterministic.
        ordered = sorted(
            self.entries,
            key=lambda record: (-(record.display_year or 0), record.display_title.casefold()),
        )
        for record in ordered:
            bibtex = record.effective_bibtex
            if not bibtex or not bibtex.strip():
                continue
            key = extract_bibtex_key(bibtex)
            if key:
                if key in used_keys:
                    warnings.append(
                        f"引用键重复：{key}（{used_keys[key]} 与 {record.title}）"
                    )
                else:
                    used_keys[key] = record.title
            blocks.append(bibtex)

        source = self.markdown_path or Path(f"{self.name}.md")
        write_bibtex(
            target,
            merge_entries(blocks),
            input_path=source,
            total=len(self.entries),
            found=len(blocks),
        )
        self.output_path = target
        return target, len(blocks), len(self.entries), warnings

    # ---------------------------------------------------------------- counts
    def counts(self) -> dict[str, int]:
        counts = {
            STATUS_FOUND: 0,
            STATUS_MISSING: 0,
            STATUS_MANUAL: 0,
            STATUS_PENDING: 0,
            "arxiv": 0,
        }
        for record in self.entries:
            counts[record.status] += 1
            if record.is_arxiv_only and record.effective_bibtex:
                counts["arxiv"] += 1
        counts["total"] = len(self.entries)
        return counts


def _project_name(path: Path) -> str:
    name = path.name
    if name.endswith(PROJECT_SUFFIX):
        name = name[: -len(PROJECT_SUFFIX)]
    else:
        name = path.stem
    return name or "未命名工程"


def _optional_path(value: Any) -> Path | None:
    text = str(value).strip() if value else ""
    return Path(text).expanduser() if text else None


def build_client(options: ProjectOptions) -> tuple[OpenAlexClient, JsonCache]:
    """Create an OpenAlex client + cache from the project settings."""

    cache = JsonCache(
        default_cache_path() if options.use_cache else None,
        ttl_days=options.cache_ttl,
        enabled=options.use_cache,
    )
    client = OpenAlexClient(
        api_key=options.api_key or os.environ.get("OPENALEX_API_KEY"),
        mailto=options.mailto or os.environ.get("OPENALEX_MAILTO"),
        per_page=options.per_page,
        cache=cache,
    )
    return client, cache
