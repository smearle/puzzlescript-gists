"""Reachability check of every external source from this machine.

Prints one line per source (HTTP status, a parsed count, latency) so a run's log
shows at a glance whether GitHub, gist search, itch.io, the Google Group or the
Wayback Machine is blocking this runner. Informational: each source that looks blocked
gets a GitHub Actions warning. The one hard failure is the GitHub API rejecting
the token (401), e.g. an expired GH_SCRAPE_TOKEN: every gist download would then
fail while the run still looked green, so that exits 1.
"""
from __future__ import annotations

import os
import re
import sys
import time

import requests

from ps_scraper import ghapi, scrape_itch, trawl_gists_html

UA = scrape_itch.UA
PROBE_GIST = "6841219"      # Microban, in the PuzzleScript gallery
PROBE_USER = "increpare"


def _probe(name, fn):
    t0 = time.time()
    try:
        status, detail, ok = fn()
    except Exception as e:  # noqa: BLE001 - report, never raise
        status, detail, ok = "error", f"{type(e).__name__}: {e}"[:200], False
    print(f"  {name:24} {str(status):6} {time.time() - t0:5.1f}s  {detail}", flush=True)
    if not ok and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{name}: {status} {detail}")
    return ok


def main() -> None:
    token = ghapi.get_token(required=False)
    api = requests.Session()
    api.headers.update(ghapi.auth_headers(token))
    print(f"external sources (GitHub token: {'set' if token else 'none'}):")

    rejected = []

    def rate_limit():
        r = api.get("https://api.github.com/rate_limit", timeout=30)
        if r.status_code == 401:
            rejected.append(r.status_code)
        core = r.json().get("resources", {}).get("core", {}) if r.ok else {}
        return r.status_code, f"core limit={core.get('limit')} remaining={core.get('remaining')}", r.ok

    def gist():
        r = api.get(f"https://api.github.com/gists/{PROBE_GIST}", timeout=30)
        n = len((r.json().get("files") or {})) if r.ok else 0
        return r.status_code, f"/gists/{PROBE_GIST}: {n} file(s)", r.ok and n > 0

    def user_gists():
        r = api.get(f"https://api.github.com/users/{PROBE_USER}/gists", params={"per_page": 5}, timeout=30)
        n = len(r.json()) if r.ok else 0
        return r.status_code, f"/users/{PROBE_USER}/gists: {n} listed", r.ok and n > 0

    def gist_search():
        s = trawl_gists_html.build_session(token)
        url = trawl_gists_html.with_page_number(trawl_gists_html.DEFAULT_SEARCH_URL, 1)
        r = s.get(url, timeout=30)
        n = len(trawl_gists_html.extract_search_results(r.text, 1)) if r.ok else 0
        return r.status_code, f"page 1: {n} gists", r.ok and n > 0

    def itch():
        r = requests.get("https://itch.io/games/newest/made-with-puzzlescript", headers={"User-Agent": UA}, timeout=30)
        n = len(scrape_itch.parse_listing_games(r.text, r.url)) if r.ok else 0
        return r.status_code, f"newest listing: {n} games", r.ok and n > 0

    def google_group():
        r = requests.get("https://groups.google.com/g/puzzlescript", headers={"User-Agent": UA}, timeout=30)
        n = len(set(re.findall(r"/g/puzzlescript/c/([A-Za-z0-9_-]{8,})", r.text))) if r.ok else 0
        return r.status_code, f"{n} topic ids on the group page", r.ok and n > 0

    def wayback():
        r = requests.get("https://web.archive.org/cdx/search/cdx",
                         params={"url": "puzzlescript.net/play.html", "matchType": "prefix",
                                 "fl": "original", "output": "json", "limit": "5"}, timeout=120)
        n = max(0, len(r.json()) - 1) if r.ok else 0
        return r.status_code, f"CDX: {n} rows", r.ok and n > 0

    def itch_game():
        # A game whose page embeds its PuzzleScript source (extracted on host 209).
        url = "https://jonbro.itch.io/candy-bomb"
        r = scrape_itch.try_get(url)
        if r is None:
            return "error", "game page not served", False
        src = scrape_itch.get_embedded_source(r.text, url)
        return r.status_code, f"game page + embed: {len(src or '')} chars of source", bool(src)

    def raw_gist():
        g = api.get(f"https://api.github.com/gists/{PROBE_GIST}", timeout=30)
        files = (g.json().get("files") or {}) if g.ok else {}
        raw = (files.get("script.txt") or next(iter(files.values()), {})).get("raw_url")
        if not raw:
            return g.status_code, "no raw_url from the API", False
        r = requests.get(raw, timeout=30)  # gist.githubusercontent.com, as the author sweep fetches it
        title = re.search(r"(?im)^\s*title\s+(.+?)\s*$", r.text or "")
        return r.status_code, f"raw gist title: {title.group(1) if title else None}", r.ok

    for name, fn in [("GitHub API rate limit", rate_limit), ("GitHub API gist", gist),
                     ("GitHub API user gists", user_gists), ("raw gist content", raw_gist),
                     ("gist search (HTML)", gist_search), ("itch.io listing", itch),
                     ("itch.io game page", itch_game),
                     ("Google Group", google_group), ("Wayback CDX", wayback)]:
        _probe(name, fn)
    if rejected:
        print("::error::the GitHub API rejected the token (401 Bad credentials); "
              "renew the GH_SCRAPE_TOKEN secret (or delete it to use the workflow token)")
        sys.exit(1)


if __name__ == "__main__":
    main()
