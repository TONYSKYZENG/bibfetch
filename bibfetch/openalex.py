"""Small OpenAlex/DOI HTTP client with retries and caching."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .cache import JsonCache

LOGGER = logging.getLogger(__name__)

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_SELECT_FIELDS = ",".join(
    (
        "id",
        "doi",
        "title",
        "publication_year",
        "publication_date",
        "type",
        "authorships",
        "primary_location",
        "locations",
        "biblio",
        "ids",
    )
)


class ApiError(RuntimeError):
    """Raised when a remote metadata service cannot satisfy a request."""


class OpenAlexClient:
    """Search OpenAlex and resolve DOIs to publisher-provided BibTeX."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        mailto: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        cache: JsonCache | None = None,
        per_page: int = 25,
    ) -> None:
        self.api_key = api_key or None
        self.mailto = mailto or None
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.cache = cache
        self.per_page = max(1, min(200, per_page))
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ HTTP

    def _user_agent(self) -> str:
        if self.mailto:
            return f"bibfetch/1.0 (mailto:{self.mailto})"
        return "bibfetch/1.0"

    def _wait_for_rate_limit(self) -> None:
        # Stay comfortably below OpenAlex's 10 requests/second limit.
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 0.11:
            time.sleep(0.11 - elapsed)

    def _open(self, url: str, *, accept: str = "application/json") -> str:
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": self._user_agent(),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self._last_request_at = time.monotonic()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:300]
                    except OSError:
                        pass
                    raise ApiError(f"HTTP {exc.code} for {url}: {detail}".strip()) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = self._retry_delay(attempt, retry_after)
                LOGGER.warning("HTTP %s from %s; retrying in %.1fs", exc.code, url, delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                delay = self._retry_delay(attempt, None)
                LOGGER.warning("Request failed (%s); retrying in %.1fs", exc, delay)

            if attempt < self.max_retries:
                time.sleep(delay)

        raise ApiError(f"request failed after {self.max_retries + 1} attempts: {url}") from last_error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(30.0, 1.5 * (2**attempt))

    def _request_json(self, url: str) -> dict[str, Any]:
        cached = self.cache.get("openalex", url) if self.cache else None
        if isinstance(cached, dict):
            return cached

        body = self._open(url, accept="application/json")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ApiError(f"OpenAlex returned invalid JSON for {url}") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"OpenAlex returned an unexpected payload for {url}")
        if self.cache:
            self.cache.set("openalex", url, payload)
        return payload

    def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = dict(params)
        query["per-page"] = query.get("per-page", self.per_page)
        query["select"] = _SELECT_FIELDS
        if self.mailto:
            query["mailto"] = self.mailto
        if self.api_key:
            query["api_key"] = self.api_key

        url = f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(query)}"
        payload = self._request_json(url)
        results = payload.get("results") or []
        if not isinstance(results, list):
            return []
        return [work for work in results if isinstance(work, dict)]

    # --------------------------------------------------------------- Search

    def search_title(self, title: str) -> list[dict[str, Any]]:
        """Phrase-search the title field (the precise first attempt)."""

        query_title = title.replace('"', '\\"')
        return self._query(
            {
                "filter": f'title.search:"{query_title}"',
                "per-page": self.per_page,
            }
        )

    def search_broad(self, title: str) -> list[dict[str, Any]]:
        """Fallback keyword search when title phrase-search finds nothing."""

        return self._query(
            {
                "search": title,
                "per-page": self.per_page,
            }
        )

    # ------------------------------------------------------------------ DOI

    def fetch_bibtex(self, doi: str) -> str:
        """Resolve a DOI to publisher BibTeX via DOI content negotiation."""

        normalized = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        if not normalized:
            raise ApiError("empty DOI")

        cache_key = f"doi:{normalized.lower()}"
        cached = self.cache.get("bibtex", cache_key) if self.cache else None
        if isinstance(cached, str) and cached.strip().startswith("@"):
            return cached

        urls = [f"https://doi.org/{urllib.parse.quote(normalized, safe='/:;()<>._-~')}"]
        # Crossref's transform endpoint is a useful fallback for Crossref DOIs.
        if not normalized.lower().startswith("10.48550/"):
            crossref_doi = urllib.parse.quote(normalized, safe="")
            fallback = f"https://api.crossref.org/works/{crossref_doi}/transform/application/x-bibtex"
            if self.mailto:
                fallback += "?" + urllib.parse.urlencode({"mailto": self.mailto})
            urls.append(fallback)

        errors: list[str] = []
        for url in urls:
            try:
                body = self._open(url, accept="application/x-bibtex; charset=utf-8").strip()
            except ApiError as exc:
                errors.append(str(exc))
                continue
            if body.startswith("@"):
                if self.cache:
                    self.cache.set("bibtex", cache_key, body)
                return body
            errors.append(f"non-BibTeX response from {url}")

        raise ApiError("could not fetch BibTeX for DOI " + normalized + ": " + " | ".join(errors))
