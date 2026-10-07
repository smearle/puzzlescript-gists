"""GitHub token lookup and a rate-limit-aware GET for the REST API.

The token comes from $GITHUB_TOKEN (the workflow sets it from the
GH_SCRAPE_TOKEN secret), else from a local `gh` login (~/.config/gh/hosts.yml),
as in script-doctor. Without one, calls are unauthenticated (60 requests/hour per
IP). The workflow's built-in GITHUB_TOKEN is no substitute: it is an app
installation token, and the gists endpoints answer it with 403.

`api_get` returns the final `requests.Response` exactly like `requests.get`
would, so callers keep their original status-code handling. It only adds
bounded waiting: on a primary rate limit it sleeps until the window resets, on a
secondary rate limit it honours Retry-After (or waits a minute), and it retries
connection errors and 5xx responses with backoff. A 403 that is not a rate limit
(e.g. a token that may not read the endpoint) is returned immediately rather
than retried forever.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_RATE_LIMIT_SLEEP = 3700  # one primary window (+ slack)


def get_token(required: bool = True) -> str | None:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.is_file():
        for line in hosts.read_text().splitlines():
            line = line.strip()
            if line.startswith("oauth_token:"):
                return line.split(":", 1)[1].strip()
    if required:
        raise SystemExit("No GitHub token: set $GITHUB_TOKEN or run `gh auth login`.")
    return None


def warn(msg: str) -> None:
    """Print a warning (also as a GitHub Actions annotation when running there)."""
    print(msg, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)


def auth_headers(token: str | None) -> dict:
    h = dict(API_HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


_SESSION: requests.Session | None = None


def session() -> requests.Session:
    """A shared session carrying the API headers + token (lazily created)."""
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(auth_headers(get_token(required=False)))
    return _SESSION


def rate_limit_wait(r: requests.Response) -> float | None:
    """Seconds to wait before retrying a 403/429, or None if it is not a rate limit."""
    retry_after = r.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after) + 1
        except ValueError:
            return 60.0
    if r.headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset = float(r.headers.get("X-RateLimit-Reset", "0"))
        except ValueError:
            reset = 0.0
        return min(MAX_RATE_LIMIT_SLEEP, max(1.0, reset - time.time() + 2))
    if r.status_code == 429:
        return 60.0
    body = (r.text or "")[:2000].lower()
    if "rate limit" in body:  # secondary rate limit without headers
        return 60.0
    return None


def api_get(url: str, *, params=None, headers=None, timeout: float = 60,
            sess: requests.Session | None = None, max_attempts: int = 6) -> requests.Response:
    """GET with bounded, rate-limit-aware retries. Raises requests.RequestException
    only if every attempt failed at the connection level."""
    s = sess or session()
    r = None
    for attempt in range(max_attempts):
        last = attempt == max_attempts - 1
        try:
            r = s.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException:
            if last:
                raise
            time.sleep(min(60, 2 ** attempt))
            continue
        if r.status_code in (403, 429):
            wait = rate_limit_wait(r)
            if wait is None or last:
                return r
            print(f"  [github] {r.status_code} rate limited; sleeping {wait:.0f}s "
                  f"(remaining={r.headers.get('X-RateLimit-Remaining')})", file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if r.status_code >= 500 and not last:
            time.sleep(min(60, 2 ** attempt))
            continue
        return r
    return r
