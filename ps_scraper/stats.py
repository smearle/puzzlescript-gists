"""Write stats/latest.json: a small record of the latest workflow run.

The workflow commits this file after every run. Besides being a handy status
line, the commit keeps the repository active, so GitHub does not disable the
scheduled workflow after 60 days without commits.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from ps_scraper import paths


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-dir", type=Path, default=paths.WORK_DIR)
    ap.add_argument("--out", type=Path, default=paths.REPO_ROOT / "stats" / "latest.json")
    ap.add_argument("--outcome", default="unknown", help="job status: success, failure or cancelled")
    args = ap.parse_args()

    run = _load(args.work_dir / "run.json")
    pub = _load(args.work_dir / "summary.json")
    base = _load(args.work_dir / "baseline.json")
    now = dt.datetime.now(dt.timezone.utc)
    before = run.get("master_games_before", base.get("master_games"))
    after = run.get("master_games_after")
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    stats = {
        "run_date": now.strftime("%Y-%m-%d"),
        "finished_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": os.environ.get("GITHUB_EVENT_NAME", "local"),
        "mode": run.get("mode"),
        "dry_run": os.environ.get("DRY_RUN", "false") == "true",
        "outcome": args.outcome,
        "games_before": before,
        "games_after": after,
        "new_games": (after - before) if isinstance(after, int) and isinstance(before, int) else None,
        "dataset_rebuilt": run.get("dataset_rebuilt"),
        "dataset_rows": pub.get("dataset_rows"),
        "dedup_representatives": pub.get("dedup_representatives"),
        "excluded_ps_plus": pub.get("excluded_ps_plus"),
        "published": pub.get("published"),
        "hf_commit": pub.get("hf_commit"),
        "github_api_token": os.environ.get("GITHUB_TOKEN_SOURCE"),
        "workflow_run": f"{server}/{repo}/actions/runs/{run_id}" if run_id else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
