"""Small OpenAlex/DOI HTTP client with retries and caching."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .cache import JsonCache

LOGGER = logging.getLogger(__name__)


def create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context that finds root certificates across macOS, Linux, and Windows.

    Specifically resolves the classic macOS Python issue:
    `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate`.
    """
    # 1. Try certifi if installed
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    # 2. Try known OS root CA bundle paths (crucial on macOS with python.org Python)
    ca_bundles = [
        "/etc/ssl/cert.pem",  # macOS / Alpine Linux
        "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu / Arch
        "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora / RHEL / CentOS
        "/etc/ssl/ca-bundle.pem",  # OpenSUSE
    ]
    for ca_path in ca_bundles:
        if os.path.isfile(ca_path):
            try:
                return ssl.create_default_context(cafile=ca_path)
            except Exception:
                continue

    # 3. Default context
    try:
        return ssl.create_default_context()
    except Exception:
        pass

    # 4. Fallback unverified context if all else fails
    LOGGER.warning("无法加载标准 SSL 证书，回退到未校验模式")
    return ssl._create_unverified_context()


def get_user_config_path() -> Path:
    """Return path to user config file: ~/.config/bibfetch/config.json."""
    return Path.home() / ".config" / "bibfetch" / "config.json"


def load_user_config() -> dict[str, Any]:
    """Load user configuration from ~/.config/bibfetch/config.json."""
    path = get_user_config_path()
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_user_config(config: dict[str, Any]) -> None:
    """Save user configuration to ~/.config/bibfetch/config.json."""
    path = get_user_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = load_user_config()
        current.update(config)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        LOGGER.warning("无法保存配置文件 %s: %s", path, exc)


def detect_default_email() -> str:
    """Detect default email from config, env, or git config."""
    cfg = load_user_config()
    if cfg.get("mailto"):
        return str(cfg["mailto"]).strip()
    if os.environ.get("OPENALEX_MAILTO"):
        return os.environ["OPENALEX_MAILTO"].strip()
    try:
        import subprocess

        output = subprocess.check_output(
            ["git", "config", "user.email"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
        if output and "@" in output:
            return output
    except Exception:
        pass
    return ""


def detect_default_api_key() -> str:
    """Detect default OpenAlex API key from env or config."""
    if os.environ.get("OPENALEX_API_KEY"):
        return os.environ["OPENALEX_API_KEY"].strip()
    cfg = load_user_config()
    if cfg.get("api_key"):
        return str(cfg["api_key"]).strip()
    return ""


def request_free_api_key(email: str) -> dict[str, Any]:
    """Request a free OpenAlex API key for the given email via POST /api-keys.

    OpenAlex automatically sends an activation email with a confirmation link.
    Once clicked, the key works with $1.00/day free quota.
    """
    email = email.strip()
    if not email or "@" not in email:
        raise ValueError("请输入有效的邮箱地址")

    url = "https://api.openalex.org/api-keys"
    req = urllib.request.Request(
        url,
        data=json.dumps({"email": email}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "bibfetch/1.0",
        },
    )
    ctx = create_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=15.0, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("api_key"):
                save_user_config({"api_key": data["api_key"], "mailto": email})
            return data
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body
        raise ApiError(f"申请 API Key 失败 (HTTP {exc.code}): {msg}") from exc
    except Exception as exc:
        raise ApiError(f"请求 OpenAlex 失败: {exc}") from exc


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
        if api_key is None:
            api_key = detect_default_api_key()
        if mailto is None:
            mailto = detect_default_email()
        self.api_key = api_key or None
        self.mailto = mailto or None
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.cache = cache
        self.per_page = max(1, min(200, per_page))
        self._last_request_at = 0.0
        self._ssl_context = create_ssl_context()

    # ------------------------------------------------------------------ HTTP

    def _user_agent(self) -> str:
        if self.mailto:
            return f"bibfetch/1.0 (mailto:{self.mailto})"
        return "bibfetch/1.0"

    def _wait_for_rate_limit(self) -> None:
        # Rate limit based on authorization: faster with api_key, standard with mailto, conservative anonymous
        interval = 0.10 if self.api_key else (0.20 if self.mailto else 0.50)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _open(self, url: str, *, accept: str = "application/json") -> str:
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            headers = {
                "Accept": accept,
                "User-Agent": self._user_agent(),
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            request = urllib.request.Request(
                url,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self._ssl_context
                ) as response:
                    self._last_request_at = time.monotonic()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                body_text = ""
                try:
                    body_text = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass

                if exc.code == 403 and "key_not_activated" in body_text:
                    raise ApiError(
                        "OpenAlex API Key 尚未激活！请前往邮箱查收标题为 "
                        "\"Activate your OpenAlex API key\" 的邮件并点击激活链接。"
                    ) from exc

                if exc.code == 429:
                    retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
                    retry_after_val = 0.0
                    if retry_after_raw:
                        try:
                            retry_after_val = float(retry_after_raw)
                        except ValueError:
                            pass

                    is_budget_exhausted = (
                        "Insufficient budget" in body_text
                        or "dailyRemainingUsd" in body_text
                        or retry_after_val > 120.0
                    )
                    if is_budget_exhausted:
                        raise ApiError(
                            "OpenAlex API 匿名配额已耗尽 (HTTP 429 Insufficient budget)。"
                            "请在设置中配置免费 API Key（可在设置中一键申请或激活邮件）。"
                        ) from exc

                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise ApiError(f"HTTP {exc.code} for {url}: {body_text[:300]}".strip()) from exc

                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = self._retry_delay(attempt, retry_after)
                LOGGER.warning("HTTP %s from %s; retrying in %.1fs", exc.code, url, delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                err_msg = str(exc)
                if "CERTIFICATE_VERIFY_FAILED" in err_msg or "certificate verify failed" in err_msg:
                    LOGGER.warning("检测到 SSL 证书验证失败，自动回退到信任系统证书/降级证书模式...")
                    self._ssl_context = ssl._create_unverified_context()
                    delay = 0.2
                else:
                    delay = self._retry_delay(attempt, None)
                    LOGGER.warning("Request failed (%s); retrying in %.1fs", exc, delay)

            if attempt < self.max_retries:
                time.sleep(delay)

        raise ApiError(f"request failed after {self.max_retries + 1} attempts: {url}") from last_error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                val = float(retry_after)
                if val <= 10.0:
                    return max(0.1, val)
                return 5.0
            except ValueError:
                pass
        return min(10.0, 1.2 * (2**attempt))

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
