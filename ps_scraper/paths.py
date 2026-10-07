"""Where the scraper keeps its state.

Everything the pipeline reads or writes lives under one state directory
(``$PS_STATE_DIR``, default ``./state``), which is persisted between runs as the
tarballs under ``state/`` in the Hugging Face dataset repo:

    master/               the corpus: one <gist_id>.txt (or fallback-named) file
                          per game, plus manifest.jsonl, provenance/,
                          provenance_report.json, dedup_cache.jsonl,
                          dedup_master.json and _quarantine_ps_plus/
    sources/lavelle/      Lavelle's gist dump (<owner>_<gist_id>.txt), static
    sources/gist_trawl/   recent-gist search trawl: <gist_id>.txt + manifest.jsonl
    sources/gist_staging/ per-source gist downloads: pedro/ (static), users/,
                          wayback/, forum/ (<gist_id>.txt + _manifest.jsonl,
                          _rejected.txt negative caches) and ps_urls.txt
    sources/itch/         itch.io scrape: TITLE_by_AUTHOR.txt + _itch_manifest.jsonl
    sources/increpare/    title-named increpare corpus, static
    sources/pedro_archive/ title-named Pedro's PuzzleScript Archive corpus, static
    sources/gallery/      PuzzleScript gallery demo games, static
    sources/games_dat.js  PuzzleScript gallery index (title -> gist id), static

In script-doctor these were, respectively: ~/puzzlescript-gists,
puzzlescript-analysis/raw_data/PuzzleScript, puzzlescript-analysis/raw_data/gists_trawl,
data/ps_dataset_staging, data/scraped_games_itchio, data/scraped_games_increpare,
data/scraped_games, PuzzleScript/src/demo and PuzzleScript/src/games_dat.js.

Scratch outputs (the Hugging Face staging folder, the PS+ removal list, logs)
go under ``$PS_WORK_DIR`` (default ``./work``).
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STATE_DIR = Path(os.environ.get("PS_STATE_DIR") or REPO_ROOT / "state").resolve()
WORK_DIR = Path(os.environ.get("PS_WORK_DIR") or REPO_ROOT / "work").resolve()

MASTER_DIR = STATE_DIR / "master"
SOURCES_DIR = STATE_DIR / "sources"

LAVELLE_DIR = SOURCES_DIR / "lavelle"
TRAWL_DIR = SOURCES_DIR / "gist_trawl"
STAGING_DIR = SOURCES_DIR / "gist_staging"
ITCH_DIR = SOURCES_DIR / "itch"
INCREPARE_DIR = SOURCES_DIR / "increpare"
PEDRO_ARCHIVE_DIR = SOURCES_DIR / "pedro_archive"
GALLERY_DIR = SOURCES_DIR / "gallery"
GAMES_DAT_JS = SOURCES_DIR / "games_dat.js"
