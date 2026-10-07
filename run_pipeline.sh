#!/usr/bin/env bash
# PuzzleScript gist dataset sweep: discover new games, fold them into the master
# corpus, and (only if the corpus changed) rebuild the Hugging Face dataset files
# in $PS_WORK_DIR/hf_stage. Uploading is a separate step:
#   python -m ps_scraper.state publish
#
# Port of script-doctor's scripts/data/daily_update.sh; same steps and order.
#
# Environment:
#   PS_STATE_DIR     state directory (default ./state; fill it with `python -m ps_scraper.state fetch`)
#   PS_WORK_DIR      scratch outputs (default ./work)
#   FULL             1 = also run the weekly sweep, 0 = never, auto (default) = on Sundays (UTC)
#   TRAWL_MAX_PAGES  gist search pages to scan at most (default 100)
#   DEDUP_WORKERS    parallel parser processes for new games (default: nproc)
#   GITHUB_TOKEN     GitHub token for the REST API (else a local `gh` login, else unauthenticated)
#   PYTHON           interpreter (default python3)
set -uo pipefail
cd "$(dirname "$0")" || exit 1
export PS_STATE_DIR="${PS_STATE_DIR:-$PWD/state}"
export PS_WORK_DIR="${PS_WORK_DIR:-$PWD/work}"
P="${PYTHON:-python3}"
MASTER="$PS_STATE_DIR/master"
FULL="${FULL:-auto}"
DEDUP_WORKERS="${DEDUP_WORKERS:-$(nproc)}"

warn() {  # a failed discovery step is logged and the run continues (as on host 209)
  echo "$1"
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::warning::$1"; fi
}
die() {
  echo "$1"
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::error::$1"; fi
  exit 1
}

[ -d "$MASTER" ] || die "no state at $PS_STATE_DIR (run: $P -m ps_scraper.state fetch)"
mkdir -p "$PS_WORK_DIR"
rm -rf "$PS_WORK_DIR/hf_stage" "$PS_WORK_DIR/ps_plus_remove.txt" "$PS_WORK_DIR/run.json"
echo "===== run_pipeline $(date -u +%FT%TZ) ====="

read -r before before_digest < <("$P" -m ps_scraper.state digest --master "$MASTER") \
  || die "could not read the master corpus"

# 1. Discovery: recent-gist marker search (early-stops once it hits seen pages).
"$P" -u -m ps_scraper.trawl_gists_html --max-pages "${TRAWL_MAX_PAGES:-100}" || warn "trawl failed"

# 2. Weekly (Sundays), or with FULL=1: the slow-moving discovery sources (PuzzleScript
#    Google Group, Internet Archive play links, newest itch.io PuzzleScript games), then
#    the author-enumeration closure, the heavy step. Consolidate first so owners found
#    by those sources seed the enumeration.
mode=daily
if [ "$FULL" = "1" ] || { [ "$FULL" = "auto" ] && [ "$(date -u +%u)" = "7" ]; }; then
  mode=full
  "$P" -u -m ps_scraper.build_ps_dataset forum || warn "forum failed"
  "$P" -u -m ps_scraper.build_ps_dataset wayback || warn "wayback failed"
  "$P" -u -m ps_scraper.scrape_itch --listing https://itch.io/games/newest/made-with-puzzlescript \
    --pages 5 --skip-known || warn "itch failed"
  "$P" -u -m ps_scraper.build_ps_dataset consolidate || warn "consolidate failed"
  "$P" -u -m ps_scraper.build_ps_dataset authors || warn "authors failed"
fi

# 3. Fold everything into the master (order matters: consolidate rewrites the
#    gist-keyed manifest, then reconcile appends variants, then backfill links).
"$P" -u -m ps_scraper.build_ps_dataset consolidate || die "consolidate failed"
"$P" -u -m ps_scraper.build_ps_dataset reconcile --add || warn "reconcile failed"
"$P" -u -m ps_scraper.backfill_fallback_gists || warn "backfill failed"

read -r after after_digest < <("$P" -m ps_scraper.state digest --master "$MASTER") \
  || die "could not read the master corpus"
echo "master: $before -> $after"

# 4. Rebuild the dataset files only if the corpus changed. Any failure here fails the
#    run, so neither the dataset nor the state is published from a half-built stage.
rebuilt=false
if [ "$after_digest" != "$before_digest" ]; then
  "$P" -u -m ps_scraper.detect_non_vanilla --master-dir "$MASTER" \
    --emit-removal-list "$PS_WORK_DIR/ps_plus_remove.txt" || die "detect_non_vanilla failed"
  "$P" -u -m ps_scraper.dedup_master --master-dir "$MASTER" --workers "$DEDUP_WORKERS" \
    || die "dedup_master failed"
  "$P" -u -m ps_scraper.build_hf_dataset --master "$MASTER" --stage "$PS_WORK_DIR/hf_stage" \
    --ps-plus-list "$PS_WORK_DIR/ps_plus_remove.txt" || die "build_hf_dataset failed"
  rebuilt=true
  echo "dataset rebuilt in $PS_WORK_DIR/hf_stage ($after games)"
else
  echo "no change to the corpus; dataset rebuild skipped"
fi

cat > "$PS_WORK_DIR/run.json" <<EOF
{"mode": "$mode", "master_games_before": $before, "master_games_after": $after, "dataset_rebuilt": $rebuilt}
EOF
echo "===== done $(date -u +%FT%TZ) ====="
