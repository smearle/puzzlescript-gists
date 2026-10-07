"""Discover newly published PuzzleScript gists via GitHub's gist search page.

Adapted from smearle/puzzlescript-analysis `trawl_gists_html.py`. Searches
gist.github.com for the line every PuzzleScript editor save carries ("Play this
game by pasting the script in http://www.puzzlescript.net/editor.html"), sorted by
last update, stops after two consecutive pages with nothing new, and downloads
each new gist through the REST API into sources/gist_trawl/<gist_id>.txt (+ a
manifest row).

Changes from the original: paths come from ps_scraper.paths; the per-owner
"clean_data" copies (unused by the dataset) are only written with --clean-dir;
429 retries on the search page are capped; API calls go through ghapi.api_get
(rate-limit aware).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from ps_scraper import ghapi, paths


DEFAULT_SEARCH_URL = (
    "https://gist.github.com/search"
    "?o=desc&q=Play+this+game+by+pasting+the+script+in+http%3A%2F%2Fwww.puzzlescript.net%2Feditor.html"
    "&s=updated"
)
DEFAULT_RAW_DIR = str(paths.TRAWL_DIR)
DEFAULT_CLEAN_DIR = None  # script-doctor wrote per-owner copies to puzzlescript-analysis/clean_data
DEFAULT_MANIFEST_PATH = os.path.join(DEFAULT_RAW_DIR, "manifest.jsonl")
DEFAULT_LAVELLE_DIR = str(paths.LAVELLE_DIR)
REQUEST_TIMEOUT_SECONDS = 30
SEARCH_SLEEP_SECONDS = 1.0
API_SLEEP_SECONDS = 0.2
RATE_LIMIT_SLEEP_SECONDS = 60.0
MAX_SEARCH_RATE_LIMIT_RETRIES = 10
HASH_RE = re.compile(r"^[0-9a-fA-F]{8,40}$")
INVALID_PATH_CHARS = r'<>:"/\\|?*'
MAX_PATH_COMPONENT_LENGTH = 48
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


@dataclass(frozen=True)
class SearchResult:
    gist_id: str
    gist_url: str
    owner: str
    page_number: int


def sanitize_path_component(value: str | None) -> str:
    sanitized_chars = []
    for ch in str(value or ""):
        if ord(ch) < 32 or ord(ch) == 127 or ch in INVALID_PATH_CHARS:
            sanitized_chars.append("_")
        else:
            sanitized_chars.append(ch)

    sanitized = "".join(sanitized_chars).strip().rstrip(". ")
    if sanitized.upper() in WINDOWS_RESERVED_NAMES:
        sanitized = f"_{sanitized}"
    if len(sanitized) > MAX_PATH_COMPONENT_LENGTH:
        sanitized = sanitized[:MAX_PATH_COMPONENT_LENGTH].rstrip()
    return sanitized or "untitled"


def extract_title(script_text: str) -> str | None:
    for raw_line in script_text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("title "):
            return line[6:].strip() or None
    return None


def extract_author(script_text: str) -> str | None:
    for raw_line in script_text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("author "):
            return line[7:].strip() or None
    return None


def extract_gist_id_from_filename(filename: str) -> str | None:
    stem, ext = os.path.splitext(os.path.basename(filename))
    if ext.lower() != ".txt":
        return None
    return stem if HASH_RE.fullmatch(stem) else None


def load_existing_raw_ids(*directories: str) -> set[str]:
    seen_ids: set[str] = set()
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            gist_id = extract_gist_id_from_filename(entry)
            if gist_id is not None:
                seen_ids.add(gist_id)
    return seen_ids


def build_session(token: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "User-Agent": "puzzlescript-gist-trawler",
        }
    )
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        session.headers["X-GitHub-Api-Version"] = "2022-11-28"
    return session


def with_page_number(url: str, page_number: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["p"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def normalize_gist_href(href: str) -> tuple[str, str, str] | None:
    parsed = urlparse(href)
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, gist_id = parts
    if owner in {"search", "discover", "starred"}:
        return None
    if not HASH_RE.fullmatch(gist_id):
        return None
    gist_url = f"https://gist.github.com/{owner}/{gist_id}"
    return owner, gist_id, gist_url


def extract_search_results(html: str, page_number: int) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, SearchResult] = {}
    for anchor in soup.select("a[href]"):
        normalized = normalize_gist_href(anchor.get("href", ""))
        if normalized is None:
            continue
        owner, gist_id, gist_url = normalized
        results[gist_id] = SearchResult(
            gist_id=gist_id,
            gist_url=gist_url,
            owner=owner,
            page_number=page_number,
        )
    return list(results.values())


def fetch_search_results(
    session: requests.Session,
    search_url: str,
    max_pages: int | None,
    sleep_seconds: float,
    seen_ids: set[str] | None = None,
    stop_after_seen_pages: int = 2,
) -> list[SearchResult]:
    page_number = 1
    seen_ids = set() if seen_ids is None else set(seen_ids)
    collected: list[SearchResult] = []
    consecutive_seen_pages = 0

    while True:
        if max_pages is not None and page_number > max_pages:
            break

        page_url = with_page_number(search_url, page_number)
        n_rate_limited = 0
        while True:
            response = session.get(page_url, timeout=REQUEST_TIMEOUT_SECONDS)
            try:
                response.raise_for_status()
                break
            except requests.exceptions.HTTPError as err:
                if response.status_code != 429:
                    raise
                n_rate_limited += 1
                if n_rate_limited > MAX_SEARCH_RATE_LIMIT_RETRIES:
                    raise
                retry_after = response.headers.get("Retry-After")
                wait_seconds = RATE_LIMIT_SLEEP_SECONDS
                if retry_after is not None:
                    try:
                        wait_seconds = max(wait_seconds, float(retry_after))
                    except ValueError:
                        pass
                print(
                    f"[search] page={page_number} rate_limited=429 "
                    f"sleep={wait_seconds:.1f}s retrying"
                )
                time.sleep(wait_seconds)

        page_results = extract_search_results(response.text, page_number)
        if not page_results:
            print(f"[search] Page empty. Exiting search.")
            break

        new_count = 0
        for result in page_results:
            if result.gist_id in seen_ids:
                continue
            seen_ids.add(result.gist_id)
            collected.append(result)
            new_count += 1

        print(f"[search] page={page_number} results={len(page_results)} new={new_count}")
        # Results are sorted by `updated` descending, so once we hit pages that are
        # entirely already-collected we have reached previously-seen territory and can
        # stop. We require N consecutive fully-seen pages (not just the first seen gist)
        # because a re-edited old gist can float back above genuinely new ones.
        if new_count == 0:
            consecutive_seen_pages += 1
            if stop_after_seen_pages and consecutive_seen_pages >= stop_after_seen_pages:
                print(f"[search] {consecutive_seen_pages} consecutive fully-seen pages; stopping early")
                break
        else:
            consecutive_seen_pages = 0

        page_number += 1
        time.sleep(sleep_seconds)

    return collected


def choose_script_file(files_payload: dict) -> tuple[str | None, str | None]:
    if not isinstance(files_payload, dict):
        return None, None

    preferred_names = ("script.txt", "script", "game.txt")
    for preferred_name in preferred_names:
        file_info = files_payload.get(preferred_name)
        if isinstance(file_info, dict) and isinstance(file_info.get("content"), str):
            return preferred_name, file_info["content"]

    for filename, file_info in files_payload.items():
        if not isinstance(file_info, dict):
            continue
        content = file_info.get("content")
        language = str(file_info.get("language") or "")
        if isinstance(content, str) and "puzzlescript" in language.lower():
            return filename, content

    for filename, file_info in files_payload.items():
        if not isinstance(file_info, dict):
            continue
        content = file_info.get("content")
        if isinstance(content, str) and "title " in content.lower():
            return filename, content

    return None, None


def fetch_gist_payload(session: requests.Session, gist_id: str) -> dict:
    response = ghapi.api_get(
        f"https://api.github.com/gists/{gist_id}",
        timeout=REQUEST_TIMEOUT_SECONDS,
        sess=session,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected gist payload for {gist_id}: {type(payload)!r}")
    return payload


def ensure_parent_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def write_text_file(path: str, content: str, overwrite: bool) -> bool:
    if os.path.exists(path) and not overwrite:
        return False
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def resolve_clean_path(clean_dir: str, payload: dict, script_text: str, gist_id: str) -> str:
    owner = None
    owner_payload = payload.get("owner")
    if isinstance(owner_payload, dict) and owner_payload.get("login"):
        owner = str(owner_payload["login"]).strip()
    if owner and owner.lower() == "anonymous":
        owner = None

    author = extract_author(script_text)
    username_dir = owner or author or "anonymous"
    title_dir = extract_title(script_text) or payload.get("description") or gist_id
    timestamp = payload.get("created_at") or payload.get("updated_at") or f"missing_timestamp_{gist_id}"

    return os.path.join(
        clean_dir,
        sanitize_path_component(username_dir),
        sanitize_path_component(title_dir),
        f"{sanitize_path_component(timestamp)}.txt",
    )


def append_manifest_rows(manifest_path: str, rows: Iterable[dict]) -> None:
    ensure_parent_dir(manifest_path)
    with open(manifest_path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def load_existing_manifest(manifest_path: str) -> set[str]:
    if not os.path.exists(manifest_path):
        return set()
    seen_ids = set()
    with open(manifest_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            gist_id = row.get("gist_id")
            if isinstance(gist_id, str) and gist_id:
                seen_ids.add(gist_id)
    return seen_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trawl GitHub gist search results for PuzzleScript saves and download matching gists."
    )
    parser.add_argument("--search-url", default=DEFAULT_SEARCH_URL)
    parser.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    parser.add_argument("--clean-dir", default=DEFAULT_CLEAN_DIR)
    parser.add_argument("--manifest-path", default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--lavelle-dir", default=DEFAULT_LAVELLE_DIR,
                        help="Lavelle dump; its bare <gist_id>.txt files count as already seen.")
    parser.add_argument("--token", default=ghapi.get_token(required=False))
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-gists", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--urls-only", action="store_true", help="Only save the manifest of gist URLs; skip API fetches.")
    parser.add_argument("--search-sleep-seconds", type=float, default=SEARCH_SLEEP_SECONDS)
    parser.add_argument("--api-sleep-seconds", type=float, default=API_SLEEP_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = build_session(args.token)
    preloaded_seen_ids = load_existing_raw_ids(args.raw_dir, args.lavelle_dir)
    if preloaded_seen_ids:
        print(f"[search] preloaded_seen_ids={len(preloaded_seen_ids)}")

    results = fetch_search_results(
        session=session,
        search_url=args.search_url,
        max_pages=args.max_pages,
        sleep_seconds=args.search_sleep_seconds,
        seen_ids=preloaded_seen_ids,
    )
    if args.max_gists is not None:
        results = results[: args.max_gists]

    print(f"[search] collected_unique_gists={len(results)}")
    if not results:
        return 0

    manifest_seen_ids = load_existing_manifest(args.manifest_path)
    manifest_rows: list[dict] = []

    for result in results:
        row = {
            "gist_id": result.gist_id,
            "gist_url": result.gist_url,
            "owner": result.owner,
            "page_number": result.page_number,
            "search_url": args.search_url,
        }
        if result.gist_id not in manifest_seen_ids:
            manifest_rows.append(row)

    if manifest_rows:
        append_manifest_rows(args.manifest_path, manifest_rows)
        print(f"[manifest] appended={len(manifest_rows)} path={args.manifest_path}")

    if args.urls_only:
        return 0

    for index, result in enumerate(results, start=1):
        try:
            payload = fetch_gist_payload(session, result.gist_id)
        except requests.HTTPError as err:
            print(f"[gist] {result.gist_id} http_error={err}")
            continue

        owner = "anonymous"
        owner_payload = payload.get("owner")
        if isinstance(owner_payload, dict) and owner_payload.get("login"):
            owner = str(owner_payload["login"])

        filename, script_text = choose_script_file(payload.get("files", {}))
        if script_text is None:
            print(f"[gist] {result.gist_id} skipped=no_script_content")
            continue

        raw_path = os.path.join(args.raw_dir, f"{result.gist_id}.txt")
        wrote_raw = write_text_file(raw_path, script_text, overwrite=args.overwrite)

        wrote_clean = False
        if args.clean_dir:
            clean_path = resolve_clean_path(args.clean_dir, payload, script_text, result.gist_id)
            wrote_clean = write_text_file(clean_path, script_text, overwrite=args.overwrite)

        print(
            f"[gist] {index}/{len(results)} id={result.gist_id} owner={owner} "
            f"file={filename or 'unknown'} raw={'write' if wrote_raw else 'skip'} "
            f"clean={'write' if wrote_clean else ('skip' if args.clean_dir else 'off')}"
        )
        time.sleep(args.api_sleep_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
