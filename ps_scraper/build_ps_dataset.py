"""Build and maintain the unified PuzzleScript gist dataset.

The dataset is a single flat directory of game source files named by their raw
GitHub gist id (``<gist_id>.txt``), plus a ``manifest.jsonl`` index: the
``master/`` directory of the scraper state (see ps_scraper/paths.py).

Subcommands:
    forum        Gist ids linked from the PuzzleScript Google Group -> staging/forum
    wayback      Gist ids from archived puzzlescript.net play/editor links -> staging/wayback
    authors      Every public gist of every known author -> staging/users
    consolidate  Merge every gist-native source into MASTER as <gist_id>.txt and
                 (re)write manifest.jsonl. Idempotent and re-runnable.
    reconcile    Provenance/collision report for the title-named corpora; with
                 --add, fold genuinely new games into MASTER.

Sources merged by `consolidate` (any that exist), in this order:
    - Lavelle dump:  sources/lavelle/*.txt (gist id in filename, possibly with
                     an owner or ANONYMOUS_ prefix)
    - gist trawl:    sources/gist_trawl/*.txt (already <gist_id>.txt)
    - staging:       sources/gist_staging/{pedro,users,wayback,forum}/*.txt

Ported from script-doctor's scripts/data/build_ps_dataset.py
(https://github.com/smearle/script-doctor). Differences: paths come from
ps_scraper.paths; GitHub API calls go through ghapi.api_get; directory listings
are sorted so results do not depend on filesystem order; bookkeeping files whose
names start with "_" (e.g. the _rejected.txt negative caches) are not read as
games, and the misfit list is written as master/_misfits.list instead of a
"*.txt" file that would itself be picked up as a game; the retired `pedro`
subcommand (pedrosworks.com stopped serving its game list in Oct 2026; its last
download is kept in staging/pedro) and the empty `search` source are dropped.

GitHub token: read from $GITHUB_TOKEN, else from `gh` (~/.config/gh/hosts.yml).
Without one, API calls are unauthenticated (60/hour), which is enough for forum,
wayback and the daily trawl, so `authors` (~1,800 requests) is skipped instead.
"""
from argparse import ArgumentParser
from collections import Counter, defaultdict
import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

from ps_scraper import ghapi, paths

DEFAULT_MASTER = paths.MASTER_DIR
DEFAULT_STAGING = paths.STAGING_DIR

LAVELLE_DIR = paths.LAVELLE_DIR
TRAWL_DIR = paths.TRAWL_DIR

# A raw gist id is 20 or 32 lowercase hex chars (modern) or a bare integer (legacy).
GIST_ID_RE = re.compile(r"^([0-9a-f]{20}|[0-9a-f]{32}|[0-9]+)$")
ANON_PREFIXES = ("ANONYMOUS_BATCH_OLD_", "ANONYMOUS_BATCH_", "ANONYMOUS_")
TITLE_RE = re.compile(r"(?im)^\s*title\s+(.+?)\s*$")
# A GitHub login: alphanumerics and single hyphens, no underscores. Lavelle's dump also
# uses pseudo-owner prefixes that are not logins (e.g. PEDROPSI_DATABASE_<gist_id>).
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
AUTHOR_RE = re.compile(r"(?im)^\s*author\s+(.+?)\s*$")


def get_token():
    return ghapi.get_token(required=False)


def parse_lavelle_filename(stem: str):
    """Return (gist_id, owner_or_None) from a Lavelle raw filename stem.

    GitHub logins contain no underscores, so an `owner_<id>` filename splits on
    the LAST underscore. ANONYMOUS_* are literal prefixes (which do contain
    underscores) and carry no owner.
    """
    owner = None
    s = stem
    for pref in ANON_PREFIXES:
        if s.startswith(pref):
            s = s[len(pref):]
            break
    else:
        if "_" in s:
            cand_owner, cand_id = s.rsplit("_", 1)
            if GIST_ID_RE.match(cand_id):
                return cand_id, cand_owner
    if GIST_ID_RE.match(s):
        return s, owner
    return None, owner


def parse_title(text: str):
    m = TITLE_RE.search(text)
    return m.group(1).strip() if m else None


def parse_author(text: str):
    """The in-game `author` prelude line — fallback label when the gist owner
    (GitHub username) is unknown, e.g. anonymous gists."""
    m = AUTHOR_RE.search(text)
    return m.group(1).strip() if m else None


def content_hash(text: str) -> str:
    """Normalized hash so the same game from different sources collides.

    Normalize line endings, strip per-line trailing whitespace, and trim leading/
    trailing blank lines. Deliberately conservative (keeps interior blank lines and
    case) so genuinely distinct games stay distinct.
    """
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    norm = "\n".join(lines)
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


# Extensions that are never PuzzleScript source; skip fetching these during enumeration.
NON_PS_EXTS = {"py", "js", "ts", "json", "md", "html", "htm", "css", "c", "cpp", "h", "hpp",
               "java", "rb", "go", "rs", "sh", "yml", "yaml", "xml", "csv", "png", "jpg",
               "jpeg", "gif", "svg", "pdf", "zip", "ipynb", "lock", "toml", "cfg", "ini"}


PS_SECTION_HEADER_RE = re.compile(
    r"(?im)^\s*(OBJECTS|LEGEND|SOUNDS|COLLISIONLAYERS|RULES|WINCONDITIONS|LEVELS)\s*$")


def is_ps_source(text: str) -> bool:
    """True if >=3 distinct PuzzleScript section names appear as standalone header
    lines. Anchoring to whole lines rejects engine/minified JS that merely mentions
    OBJECTS/LEGEND/RULES inside code (the itch extractor's failure mode)."""
    if not text:
        return False
    found = {m.group(1).upper() for m in PS_SECTION_HEADER_RE.finditer(text)}
    return len(found) >= 3


# Title-named corpora to reconcile against the gist-keyed master, by method tag.
RECONCILE_CORPORA = [
    ("increpare", paths.INCREPARE_DIR),
    ("pedro", paths.PEDRO_ARCHIVE_DIR),
    ("itch", paths.ITCH_DIR),
    ("gallery", paths.GALLERY_DIR),
]


def _games(d: Path):
    """The *.txt game files of a source dir in sorted order, skipping bookkeeping
    files such as _rejected.txt (no gist id or Lavelle name starts with "_")."""
    return sorted(p for p in d.glob("*.txt") if not p.name.startswith("_"))


# --------------------------------------------------------------------------- #
# authors: per-user gist enumeration (recovers the Lavelle->now gap)
# --------------------------------------------------------------------------- #
def known_owners(master: Path = DEFAULT_MASTER, staging: Path = DEFAULT_STAGING) -> list:
    """Distinct known gist owners from the master + pedro manifests."""
    owners = set()
    for mf in (master / "manifest.jsonl",
               staging / "pedro" / "_manifest.jsonl"):
        if not mf.is_file():
            continue
        for line in mf.read_text().splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line).get("owner")
            except json.JSONDecodeError:
                continue
            if o and o.lower() not in ("anonymous", "invalid-email-address"):
                owners.add(o)
    return sorted(owners)


def cmd_authors(master: Path, staging: Path, limit):
    """Enumerate every public gist of each known PuzzleScript author and keep the
    ones whose content is PuzzleScript source but whose gist id we don't already
    have. /users/{u}/gists is NOT firehose-capped, so this reaches games published
    after Lavelle's snapshot and any the original filter missed.
    """
    if not get_token():
        ghapi.warn("no GitHub token: skipping the author enumeration (about 1,800 API requests; "
                   "unauthenticated calls are limited to 60 per hour). Set GH_SCRAPE_TOKEN to enable it.")
        return
    out = staging / "users"
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "_manifest.jsonl"

    have = {p.stem for p in master.glob("*.txt")}
    have |= {p.stem for p in out.glob("*.txt")}  # resume
    owners = known_owners(master, staging)
    if limit:
        owners = owners[:limit]
    print(f"enumerating {len(owners)} authors; master already holds {len(have)} gist ids")

    new_saved = 0
    for ai, owner in enumerate(owners):
        page = 1
        while True:
            try:
                # Rate limits are waited out inside api_get.
                r = ghapi.api_get(f"https://api.github.com/users/{owner}/gists",
                                  params={"per_page": 100, "page": page}, timeout=60)
            except requests.RequestException as e:
                print(f"  {owner} list error: {e}")
                break
            if r.status_code == 404:
                break  # account renamed/deleted
            if not r.ok:
                print(f"  {owner} list HTTP {r.status_code}")
                break
            gists = r.json()
            if not gists:
                break
            for g in gists:
                gid = str(g.get("id") or "")
                if not gid or gid in have:
                    continue
                # fetch candidate files via raw_url (separate host, not API-limited),
                # skipping obvious non-PS files to avoid wasted fetches.
                for fname, finfo in (g.get("files") or {}).items():
                    raw = finfo.get("raw_url")
                    if not raw:
                        continue
                    ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
                    if ext in NON_PS_EXTS:
                        continue
                    size = finfo.get("size") or 0
                    if size == 0 or size > 600_000:
                        continue
                    try:
                        rr = requests.get(raw, timeout=60)
                    except requests.RequestException:
                        continue
                    if not rr.ok or not is_ps_source(rr.text):
                        continue
                    text = rr.text
                    (out / f"{gid}.txt").write_text(text, encoding="utf-8")
                    have.add(gid)
                    new_saved += 1
                    with manifest.open("a", encoding="utf-8") as mf:
                        mf.write(json.dumps({
                            "gist_id": gid, "owner": owner, "title": parse_title(text),
                            "updated_at": g.get("updated_at"), "source": "user_enum",
                        }) + "\n")
                    break  # one PS file per gist is enough
            if len(gists) < 100:
                break
            page += 1
        if (ai + 1) % 50 == 0:
            print(f"  [{ai + 1}/{len(owners)}] authors scanned, {new_saved} new PS gists")
    print(f"authors done: {new_saved} new PS gists into {out}")


# --------------------------------------------------------------------------- #
# forum: recover gist ids from the PuzzleScript Google Group threads
# --------------------------------------------------------------------------- #
_GIST_LINK_RE = re.compile(
    r"(?:play\.html\?p=|editor\.html\?hack=|gist\.github\.com/[A-Za-z0-9_.-]+/)"
    r"([0-9a-fA-F]{20,32}|[0-9]+)")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _forum_topic_ids() -> set:
    """Topic ids for groups.google.com/g/puzzlescript: recent ones embedded in the
    live group page + historical ones from the Wayback Machine. (Exhaustive history
    would need Google's batchexecute RPC; this captures recent + archived.)"""
    tids = set()
    try:
        gp = requests.get("https://groups.google.com/g/puzzlescript",
                          headers={"User-Agent": _UA}, timeout=60)
        tids |= set(re.findall(r"/g/puzzlescript/c/([A-Za-z0-9_-]{8,})", gp.text))
    except requests.RequestException:
        pass
    for pat in ("groups.google.com/g/puzzlescript", "groups.google.com/d/topic/puzzlescript"):
        try:
            d = requests.get("https://web.archive.org/cdx/search/cdx",
                params={"url": pat, "matchType": "prefix", "collapse": "urlkey",
                        "fl": "original", "output": "json", "limit": "50000"}, timeout=180).json()
        except (requests.RequestException, ValueError):
            continue
        for row in d[1:]:
            for m in re.finditer(r"puzzlescript/(?:c|topic)/([A-Za-z0-9_-]{8,})", row[0]):
                tids.add(m.group(1))
    return tids


def cmd_forum(master: Path, staging: Path):
    out = staging / "forum"
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "_manifest.jsonl"
    have = {p.stem for p in master.glob("*.txt")} | {p.stem for p in out.glob("*.txt")}
    rejected = out / "_rejected.txt"  # gist ids already checked: deleted or not PS
    if rejected.is_file():
        have |= set(rejected.read_text().split())

    tids = _forum_topic_ids()
    print(f"{len(tids)} forum topics to scan")
    found = {}  # gist_id -> topic_id
    for i, tid in enumerate(sorted(tids)):
        try:
            r = requests.get(f"https://groups.google.com/g/puzzlescript/c/{tid}",
                             headers={"User-Agent": _UA}, timeout=60)
        except requests.RequestException:
            continue
        if r.ok:
            for m in _GIST_LINK_RE.finditer(r.text):
                found.setdefault(m.group(1).lower(), tid)
        time.sleep(0.3)
        if (i + 1) % 25 == 0:
            print(f"  scanned {i + 1}/{len(tids)} topics, {len(found)} gist ids so far")
    new = [g for g in found if g not in have]
    print(f"{len(found)} distinct gist ids linked in forum, {len(new)} not already held")

    saved = 0
    for gid in new:
        try:
            r = ghapi.api_get(f"https://api.github.com/gists/{gid}", timeout=60)
        except requests.RequestException:
            continue
        if r.status_code == 404:
            with rejected.open("a") as rf:
                rf.write(gid + "\n")
            continue
        if not r.ok:
            continue
        script = next((f["content"] for f in (r.json().get("files") or {}).values()
                       if f.get("content") and is_ps_source(f["content"])), None)
        if not script:
            with rejected.open("a") as rf:
                rf.write(gid + "\n")
            continue
        (out / f"{gid}.txt").write_text(script, encoding="utf-8")
        saved += 1
        with manifest.open("a", encoding="utf-8") as mf:
            mf.write(json.dumps({"gist_id": gid, "owner": (r.json().get("owner") or {}).get("login"),
                                 "title": parse_title(script), "forum_topic": found[gid],
                                 "source": "forum"}) + "\n")
    print(f"forum done: {saved} new PS gists -> {out}")


# --------------------------------------------------------------------------- #
# wayback: recover gist ids from archived puzzlescript.net play/editor links
# --------------------------------------------------------------------------- #
def cdx_gist_ids() -> dict:
    """Query the Internet Archive CDX API for every archived puzzlescript.net
    play/editor link and return {gist_id: an_archived_url}. A play link is itself
    a gist id (play.html?p=<id> / editor.html?hack=<id>), so this recovers games
    that were shared anywhere the Archive crawled — independent of GitHub.
    """
    ids = {}
    pat_re = re.compile(r"[?&](?:p|hack)=([0-9a-fA-F]{20,32}|[0-9]+)")
    for pat in ("puzzlescript.net/play.html", "puzzlescript.net/editor.html"):
        params = {"url": pat, "matchType": "prefix", "collapse": "urlkey",
                  "fl": "original", "output": "json", "limit": "200000"}
        try:
            r = requests.get("https://web.archive.org/cdx/search/cdx", params=params, timeout=300)
            r.raise_for_status()
            rows = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  CDX query for {pat} failed: {e}")
            continue
        n = 0
        for row in rows[1:]:  # row[0] is the header
            if not row:
                continue
            m = pat_re.search(row[0])
            if m:
                ids.setdefault(m.group(1).lower(), row[0])
                n += 1
        print(f"  CDX {pat}: {len(rows) - 1} archived urls, {n} with a gist id")
    return ids


def cmd_wayback(master: Path, staging: Path):
    out = staging / "wayback"
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "_manifest.jsonl"

    have = {p.stem for p in master.glob("*.txt")} | {p.stem for p in out.glob("*.txt")}
    rejected = out / "_rejected.txt"  # gist ids already checked: deleted or not PS
    if rejected.is_file():
        have |= set(rejected.read_text().split())
    ids = cdx_gist_ids()
    new_ids = [g for g in ids if g not in have]
    print(f"CDX yielded {len(ids)} distinct gist ids; {len(new_ids)} not already held")

    saved = dead = notps = 0
    for i, gid in enumerate(new_ids):
        try:
            r = ghapi.api_get(f"https://api.github.com/gists/{gid}", timeout=60)
        except requests.RequestException:
            r = None
        if r is None or r.status_code == 404:
            dead += 1
            if r is not None:
                with rejected.open("a") as rf:
                    rf.write(gid + "\n")
            continue
        if not r.ok:
            continue
        gist = r.json()
        script = None
        for f in (gist.get("files") or {}).values():
            c = f.get("content")
            if c and is_ps_source(c):
                script = c
                break
        if not script:
            notps += 1
            with rejected.open("a") as rf:
                rf.write(gid + "\n")
            continue
        (out / f"{gid}.txt").write_text(script, encoding="utf-8")
        have.add(gid)
        saved += 1
        owner = (gist.get("owner") or {}).get("login")
        with manifest.open("a", encoding="utf-8") as mf:
            mf.write(json.dumps({
                "gist_id": gid, "owner": owner, "title": parse_title(script),
                "updated_at": gist.get("updated_at"), "archived_url": ids[gid],
                "source": "wayback",
            }) + "\n")
        if saved % 50 == 0:
            print(f"  {i + 1}/{len(new_ids)} checked, {saved} saved, {dead} dead, {notps} not-ps")
    print(f"wayback done: {saved} new PS gists, {dead} deleted/404, {notps} live-but-not-ps -> {out}")


# --------------------------------------------------------------------------- #
# consolidate
# --------------------------------------------------------------------------- #
def _lavelle_order(p: Path):
    _, owner = parse_lavelle_filename(p.stem)
    return (bool(owner) and not GITHUB_LOGIN_RE.match(owner), p.name)


def _iter_sources(staging: Path):
    """Yield (gist_id, source_path, source_tag, owner_or_None)."""
    # Lavelle (name order, except that pseudo-owner copies such as
    # PEDROPSI_DATABASE_<id>.txt come after real-owner copies of the same gist)
    if LAVELLE_DIR.is_dir():
        for p in sorted(_games(LAVELLE_DIR), key=_lavelle_order):
            gid, owner = parse_lavelle_filename(p.stem)
            yield (gid, p, "lavelle", owner) if gid else (None, p, "lavelle", owner)
    # trawl + staging: filename is already the gist id
    for d, tag in ((TRAWL_DIR, "trawl"), (staging / "pedro", "pedro"),
                   (staging / "users", "users"), (staging / "wayback", "wayback"),
                   (staging / "forum", "forum")):
        if d.is_dir():
            for p in _games(d):
                gid = p.stem.lower()
                yield (gid, p, tag, None) if GIST_ID_RE.match(gid) else (None, p, tag, None)


def _load_owner_map(staging: Path) -> dict:
    """gist_id -> GitHub owner login, from the staging/trawl scrape manifests
    (Lavelle filenames carry the owner inline; these sources record it separately)."""
    out = {}
    manifests = [staging / "pedro" / "_manifest.jsonl",
                 staging / "users" / "_manifest.jsonl",
                 staging / "wayback" / "_manifest.jsonl",
                 TRAWL_DIR / "manifest.jsonl"]
    for mf in manifests:
        if not mf.is_file():
            continue
        for line in mf.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            gid, o = r.get("gist_id"), r.get("owner")
            if gid and o and str(o).lower() not in ("anonymous", "invalid-email-address"):
                out[gid.lower()] = o
    return out


def cmd_consolidate(master: Path, staging: Path):
    master.mkdir(parents=True, exist_ok=True)
    owner_map = _load_owner_map(staging)
    records = {}      # gist_id -> dict
    misfits = []
    for gid, path, tag, owner in _iter_sources(staging):
        if gid is None:
            misfits.append(str(path))
            continue
        owner = owner or owner_map.get(gid)  # backfill from scrape manifests
        rec = records.get(gid)
        if rec is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            (master / f"{gid}.txt").write_text(text, encoding="utf-8")
            records[gid] = {
                "gist_id": gid,
                "title": parse_title(text),
                "owner": owner,              # GitHub username (preferred label)
                "author": parse_author(text),  # in-game author line (fallback label)
                "sources": [tag],
                "orig_filenames": [path.name],
            }
        else:
            if tag not in rec["sources"]:
                rec["sources"].append(tag)
            if path.name not in rec["orig_filenames"]:
                rec["orig_filenames"].append(path.name)
            if owner and not rec.get("owner"):
                rec["owner"] = owner

    manifest = master / "manifest.jsonl"
    with manifest.open("w", encoding="utf-8") as mf:
        for gid in sorted(records):
            mf.write(json.dumps(records[gid], ensure_ascii=False) + "\n")

    from collections import Counter
    src_counts = Counter(s for r in records.values() for s in r["sources"])
    print(f"MASTER: {master}")
    print(f"  {len(records)} unique gist ids written")
    print(f"  per-source contributions: {dict(src_counts)}")
    print(f"  {len(misfits)} files with unparseable gist id (skipped)")
    misfit_list = master / "_misfits.list"  # not *.txt: it must not be read as a game
    if misfits:
        rel = [str(Path(m).relative_to(paths.STATE_DIR)) if Path(m).is_relative_to(paths.STATE_DIR) else m
               for m in misfits]
        misfit_list.write_text("\n".join(rel) + "\n")
        for m in rel[:20]:
            print(f"    misfit: {m}")
        print(f"  -> listed in {misfit_list}")
    elif misfit_list.exists():
        misfit_list.unlink()


# --------------------------------------------------------------------------- #
# reconcile: provenance + collision tracking for title-named corpora
# --------------------------------------------------------------------------- #
def _fallback_master_path(master: Path, stem: str, chash: str) -> Path:
    """Name for a non-gist game: <Title_by_author>_<short-content-hash>.txt.

    The content-hash suffix is deterministic and scrape-order independent, so
    same-title variants coexist and re-running reconcile never renumbers files
    (unlike a _vN counter). Chronology belongs in the manifest, not the filename.
    """
    return master / f"{stem}_{chash[:10]}.txt"


def _load_itch_gist_map(itch_dir: Path = paths.ITCH_DIR) -> dict:
    """saved_file -> trusted_gist_id, from the itch scraper manifest (if present)."""
    mf = itch_dir / "_itch_manifest.jsonl"
    out = {}
    if mf.is_file():
        for line in mf.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a partial line if the scraper is mid-write
            if r.get("saved_file") and r.get("trusted_gist_id"):
                out[r["saved_file"]] = r["trusted_gist_id"]
    return out


def cmd_reconcile(master: Path, add: bool):
    """Provenance/collision report for the title-named corpora against the gist-keyed
    master. This is bookkeeping, NOT deduplication: distinct content (including
    same-title variants) is something we KEEP. The only thing not re-imported is a
    file byte-identical to one already in the master (logged as an alias instead).

    Per-corpus file buckets:
      empty                 - normalizes to nothing (skip)
      duplicate_of_master   - exact content already held under a gist id (alias only)
      variant_same_title    - distinct content; a title we already have  -> KEPT
      variant_new_title     - distinct content; a new title              -> KEPT

    With --add, every KEPT item is copied into the master: under <gist_id>.txt when a
    trusted gist id is known (itch), else <Title_by_author>.txt with _vN suffixing.
    """
    if not master.is_dir():
        raise SystemExit(f"master {master} does not exist; run consolidate first")

    EMPTY_HASH = content_hash("")

    # Index the gist-keyed master by content hash and by title.
    master_hash = {}        # hash -> gist_id (first seen)
    master_titles = set()
    for p in sorted(master.glob("*.txt")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        master_hash.setdefault(content_hash(text), p.stem)
        t = parse_title(text)
        if t:
            master_titles.add(t.strip().lower())
    print(f"master: {len(master_hash)} distinct content hashes, {len(master_titles)} distinct titles "
          f"over {len(list(master.glob('*.txt')))} files")

    def corpus_title(fn: str) -> str:
        stem = fn[:-4] if fn.endswith(".txt") else fn
        return stem.rsplit("_by_", 1)[0].replace("_", " ").strip().lower()

    itch_gist = _load_itch_gist_map()
    discovery_log = master / "provenance"
    discovery_log.mkdir(exist_ok=True)

    added_hash = dict(master_hash)   # content already represented (master + anything we add)
    report = {}
    added_manifest = []
    for tag, d in RECONCILE_CORPORA:
        if not d.is_dir():
            continue
        buckets = Counter()
        with (discovery_log / f"{tag}.jsonl").open("w", encoding="utf-8") as lf:
            for p in sorted(d.glob("*.txt")):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                h = content_hash(text)
                fn = p.name
                title = corpus_title(fn)
                if h == EMPTY_HASH:
                    res, gid = "empty", None
                elif h in master_hash:
                    res, gid = "duplicate_of_master", master_hash[h]
                elif title and title in master_titles:
                    res, gid = "variant_same_title", None
                else:
                    res, gid = "variant_new_title", None
                buckets[res] += 1

                kept_as = None
                if (add and res in ("variant_same_title", "variant_new_title")
                        and h not in added_hash and is_ps_source(text)):
                    trusted = itch_gist.get(fn) if tag == "itch" else None
                    if trusted and not (master / f"{trusted}.txt").exists():
                        dest = master / f"{trusted}.txt"
                    else:
                        dest = _fallback_master_path(master, p.stem, h)
                    dest.write_text(text, encoding="utf-8")
                    added_hash[h] = dest.stem
                    kept_as = dest.name
                    added_manifest.append({
                        "gist_id": trusted, "title": parse_title(text),
                        "owner": None, "author": parse_author(text),
                        "sources": [tag], "orig_filenames": [fn],
                        "master_file": dest.name,
                    })

                lf.write(json.dumps({
                    "filename": fn, "content_hash": h, "gist_id": gid,
                    "resolution": res, "added_as": kept_as,
                }) + "\n")
        report[tag] = {
            "files": sum(buckets.values()),
            "empty": buckets["empty"],
            "duplicate_of_master": buckets["duplicate_of_master"],
            "variant_same_title_KEPT": buckets["variant_same_title"],
            "variant_new_title_KEPT": buckets["variant_new_title"],
        }

    if add and added_manifest:
        with (master / "manifest.jsonl").open("a", encoding="utf-8") as mf:
            for rec in added_manifest:
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"\nADDED {len(added_manifest)} distinct-content games into master "
              f"(now {len(list(master.glob('*.txt')))} files)")

    (master / "provenance_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nper-method discovery logs -> {discovery_log}/<method>.jsonl")
    print(f"summary -> {master / 'provenance_report.json'}")


if __name__ == "__main__":
    ap = ArgumentParser(description=__doc__)
    ap.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    ap.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("consolidate", help="Merge gist-native sources into MASTER")

    au = sub.add_parser("authors", help="Enumerate known authors' gists to recover missed PS games")
    au.add_argument("--limit", type=int, default=None, help="Only scan the first N authors (for testing)")

    sub.add_parser("wayback", help="Recover gist ids from archived puzzlescript.net play links (Internet Archive)")
    sub.add_parser("forum", help="Recover gist ids linked in the PuzzleScript Google Group threads")

    rc = sub.add_parser("reconcile", help="Provenance/collision report for title-named corpora")
    rc.add_argument("--add", action="store_true", help="Fold novel games into MASTER (Title_by_author.txt)")

    args = ap.parse_args()
    if args.cmd == "authors":
        cmd_authors(args.master, args.staging, args.limit)
    elif args.cmd == "wayback":
        cmd_wayback(args.master, args.staging)
    elif args.cmd == "forum":
        cmd_forum(args.master, args.staging)
    elif args.cmd == "consolidate":
        cmd_consolidate(args.master, args.staging)
    elif args.cmd == "reconcile":
        cmd_reconcile(args.master, args.add)
