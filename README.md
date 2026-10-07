# puzzlescript-gists

The scraper behind the Hugging Face dataset
[`smearle/puzzlescript-gists`](https://huggingface.co/datasets/smearle/puzzlescript-gists):
every human-authored [PuzzleScript](https://www.puzzlescript.net/) game we can find in
public GitHub gists and a few PuzzleScript archives, tagged so it can be deduplicated by
mechanics and levels. It runs once a day as a GitHub Actions workflow
([`.github/workflows/scrape.yml`](.github/workflows/scrape.yml)).

The code is ported from [script-doctor](https://github.com/smearle/script-doctor)
(PuzzleJAX), where the pipeline ran as a cron job: `scripts/data/build_ps_dataset.py`,
`scrape_itch.py`, `backfill_fallback_gists.py` and `daily_update.sh`;
`nca_wm/scripts/detect_non_vanilla.py`, `dedup_master.py` and `build_hf_dataset.py`;
and the parser stack in `puzzlescript_jax/` and `nca_wm/tokenize_game.py`, vendored
here as `ps_parse/` without JAX. The gist-search trawler comes from the
`puzzlescript-analysis` repository. MIT licensed, like script-doctor.

## Pipeline

Each run ([`run_pipeline.sh`](run_pipeline.sh)) does the following.

1. **Gist search trawl** (daily). It scrapes `gist.github.com/search` for the line that
   every PuzzleScript editor save contains, newest first, and stops after two pages with
   nothing new. Each new gist is downloaded through the REST API.
2. **Weekly sweep** (Sundays UTC, or the `full` input):
   - `forum`: gist links in the PuzzleScript Google Group threads;
   - `wayback`: puzzlescript.net play and editor links archived by the Internet Archive;
   - `itch`: the 5 newest pages of itch.io "made with PuzzleScript" games;
   - `authors`: every public gist of every known PuzzleScript author.
3. **Fold into the corpus.**
   - `consolidate` writes every gist-keyed source to `master/<gist_id>.txt` and rewrites
     the manifest.
   - `reconcile --add` compares the title-named corpora (increpare, Pedro's archive,
     itch.io, the gallery) with the master and adds games it does not already hold.
   - `backfill` links fallback-named games to the gist revision they came from.
4. **Rebuild the dataset, only if the corpus changed.**
   - Flag PuzzleScript Plus games, which the dataset leaves out.
   - Fingerprint the new games: a Lark parse gives name-invariant mechanics and level
     tokens, and a cache means only new content is parsed.
   - Group duplicates and write the dataset files.
5. **Publish.** One commit to the dataset repo uploads the rebuilt dataset files and every
   changed piece of state. Nothing is uploaded when nothing changed.
6. **Record stats.** The workflow commits [`stats/latest.json`](stats/latest.json), which
   holds the run date, game counts and what was published. These commits also stop
   GitHub disabling the schedule after 60 days without repository activity.

A failed discovery step (1 or 2) is logged as a warning and the run continues, as it did
on the old host. A failure from `consolidate` onwards fails the run, and nothing is
published.

## State

Everything the pipeline reads or writes lives in one state directory
([`ps_scraper/paths.py`](ps_scraper/paths.py)):

| path | contents |
|---|---|
| `master/` | the corpus, one file per game; `manifest.jsonl`, `provenance/`, `dedup_cache.jsonl` (parse fingerprints), `dedup_master.json`, `_quarantine_ps_plus/` |
| `sources/lavelle/` | Lavelle's gist dump (`<owner>_<gist_id>.txt`), static |
| `sources/gist_trawl/` | gist-search trawl downloads and their `manifest.jsonl` |
| `sources/gist_staging/` | `users/`, `wayback/`, `forum/` downloads with `_manifest.jsonl` and `_rejected.txt` negative caches; `pedro/` and `ps_urls.txt` (static) |
| `sources/itch/` | itch.io extractions and `_itch_manifest.jsonl` |
| `sources/increpare/`, `pedro_archive/`, `gallery/`, `games_dat.js` | title-named corpora and the gallery index, static |

The state is stored in the dataset repo itself, under `state/`, so the Hugging Face token
needs access to that one repo only:

- `state/static.tar`: the static sources;
- `state/master.tar`: `master/`;
- `state/sources.tar`: the growing sources;
- `state/STATE.json`: the sha256, size and file count of each tarball, and the game count.

The tarballs are uncompressed and byte-reproducible: members are sorted, and mtimes,
owners and modes are fixed. An unchanged group therefore hashes the same and is not
re-uploaded. When a group does change, the Hub's chunk-level deduplication stores only
the changed regions.

Each run works as follows:

1. `python -m ps_scraper.state fetch` downloads `state/` and the current dataset files.
   It checks every tarball against `STATE.json` and unpacks them.
2. The pipeline runs.
3. `python -m ps_scraper.state publish` repacks the state and compares the hashes.

The dataset and the state are always committed together. `publish` refuses to upload if
the corpus or the dataset shrank by more than 1%, which would mean lost state rather than
new data.

The state was bootstrapped on 2026-10-07 from the old cron host (`state bootstrap` and
`state upload-bootstrap`).

The manifests in the state record gist owners' GitHub usernames, as the old host's
copies did. They are public information, since any gist id resolves to its owner. The
dataset rows themselves still carry no handles.

## Secrets

| secret | used for | create it as |
|---|---|---|
| `HF_TOKEN` | uploading the dataset and state | a fine-grained Hugging Face token with write access to `datasets/smearle/puzzlescript-gists` only |
| `GH_SCRAPE_TOKEN` | the GitHub REST API (gist contents, users' gist lists) | a fine-grained GitHub personal access token: "Public repositories (read-only)", no extra permissions |

Neither secret is strictly required.

- **Without `HF_TOKEN`**, the workflow runs the whole pipeline but skips the upload, with a
  warning.
- **Without `GH_SCRAPE_TOKEN`**, GitHub API calls are unauthenticated: 60 requests per hour
  per runner IP, enough for the daily trawl. The weekly author enumeration (about 1,800
  requests) is then skipped, with a warning.

The job's built-in `GITHUB_TOKEN` is no substitute. It is an app installation token, and
the gists endpoints answer it with 403 (seen in the first dry run, 2026-10-07).

The first step of every run ("Check access to external sources") prints the GitHub
rate-limit status and checks that gist search, itch.io, the Google Group and the Wayback
Machine respond.

## Running it manually

From GitHub, open Actions → scrape → Run workflow. It has two inputs:

- `full` runs the weekly sweep;
- `dry_run` runs everything except the upload.

From a terminal:

```sh
gh workflow run scrape.yml -R smearle/puzzlescript-gists -f full=true -f dry_run=true
```

Locally (Python 3.13). The state takes about 1.3 GB.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m ps_scraper.state fetch          # -> ./state, current dataset -> ./work/published
FULL=0 TRAWL_MAX_PAGES=2 PYTHON=.venv/bin/python bash run_pipeline.sh
.venv/bin/python -m ps_scraper.state publish --dry-run   # without --dry-run (and with HF_TOKEN set) it uploads
```

`run_pipeline.sh` reads `PS_STATE_DIR`, `PS_WORK_DIR`, `FULL` (`1`, `0` or `auto`, which
means Sundays), `TRAWL_MAX_PAGES`, `DEDUP_WORKERS` and `GITHUB_TOKEN`. Without
`GITHUB_TOKEN` it falls back to a local `gh` login.

## Changes from the script-doctor version

- **Deterministic order.** Directory listings are sorted, so results no longer depend on
  filesystem order. Before, they did when two Lavelle files share a gist id. Within such a
  pair, a real-owner copy is read before a pseudo-owner copy such as
  `PEDROPSI_DATABASE_<id>.txt`. Compared with the old host, the switch changes a few
  things once:
  - one game's text: `f10c3f6fc2053b5482ea` now takes its `ANONYMOUS_BATCH_OLD_` copy,
    whose `title` line is correct, so it now parses;
  - the `orig_filenames` order of 15 manifest rows;
  - cosmetic fields: which gist id the provenance logs name for duplicate content, and
    two labels in `dedup_master.json`.
- **Bookkeeping files are not games.**
  - The `_rejected.txt` negative caches are no longer read as game sources.
  - The misfit list is written to `master/_misfits.list`. Its old name, `_misfits.txt`, was
    itself picked up as a game and published as a dataset row with id `_misfits`.
- **Rebuild trigger.** The dataset is rebuilt whenever the corpus content changes, not
  only when the file count grows. A failed rebuild fails the run, so state and dataset
  never drift apart.
- **Rate limits.** GitHub API calls wait out rate limits, with a bound. Before, they
  retried forever or silently skipped the gist.
- **Dead steps dropped.**
  - The `pedro` step is gone: pedrosworks.com stopped serving its game list in Oct 2026,
    and its last download is kept as a static source.
  - So is the empty `search` source.
  - The per-owner `clean_data/` copies the trawler wrote, which the dataset never used,
    are no longer written.
