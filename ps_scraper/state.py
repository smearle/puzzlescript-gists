"""Persist the scraper state inside the Hugging Face dataset repo, under state/.

The state directory (see ps_scraper/paths.py) is stored as three deterministic,
uncompressed tarballs plus a small index:

    state/static.tar    sources that never change (Lavelle dump, increpare,
                        Pedro's archive, gallery, pedro staging, master/_quarantine_ps_plus)
    state/master.tar    master/ (the corpus, manifest, provenance, dedupe cache)
    state/sources.tar   the growing sources (gist trawl, author/wayback/forum
                        staging, itch.io scrape)
    state/STATE.json    sha256, size and file count of each tarball, game count

Tarballs are byte-reproducible (sorted members, zeroed mtimes/owners, fixed
modes), so an unchanged group hashes identically and is not re-uploaded, and the
Hub's chunk-level deduplication stores only the changed regions of a changed one.

Subcommands:
    fetch             download state/ (+ the current dataset files) and unpack it
    digest            print "<game count> <digest>" of a master dir
    publish           pack the state and upload, in ONE commit, the rebuilt dataset
                      (if any) and every changed tarball; respects --dry-run and a
                      missing HF_TOKEN
    bootstrap         pack the legacy script-doctor/host-209 layout into tarballs
    upload-bootstrap  upload those tarballs (state/ only) to the dataset repo
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from ps_scraper import paths

HF_REPO = os.environ.get("HF_REPO", "smearle/puzzlescript-gists")
GROUPS = ("static", "master", "sources")
STATIC_PREFIXES = (
    "sources/lavelle/", "sources/increpare/", "sources/pedro_archive/", "sources/gallery/",
    "sources/games_dat.js", "sources/gist_staging/pedro/", "sources/gist_staging/ps_urls.txt",
    "master/_quarantine_ps_plus/",
)
SKIP_NAMES = {".DS_Store"}
SKIP_DIRS = {"__MACOSX", "__pycache__", ".cache"}
# Refuse to publish if the corpus or the dataset shrank by more than this fraction:
# that means lost state, not new data.
MAX_SHRINK = 0.01


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def group_of(rel: str) -> str:
    if any(rel == p or rel.startswith(p) for p in STATIC_PREFIXES):
        return "static"
    if rel.startswith("master/"):
        return "master"
    if rel.startswith("sources/"):
        return "sources"
    raise ValueError(f"file outside master/ and sources/: {rel}")


def walk_files(root: Path, skip=None) -> list[str]:
    """Relative POSIX paths of every regular file under root (sorted)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in filenames:
            if fn in SKIP_NAMES:
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if skip and skip(rel):
                continue
            if p.is_symlink() or not p.is_file():
                raise SystemExit(f"not a regular file: {p}")
            out.append(rel)
    return sorted(out)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_tar(out: Path, members: list[tuple[str, Path]]) -> dict:
    """Write a reproducible tar of (arcname, source) pairs; return its index entry."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for arcname, src in sorted(members, key=lambda m: m[0]):
            ti = tarfile.TarInfo(arcname)
            ti.size = src.stat().st_size
            ti.mtime = 0
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            with open(src, "rb") as f:
                tf.addfile(ti, f)
    return {"sha256": sha256_file(out), "bytes": out.stat().st_size, "files": len(members)}


def pack(state_dir: Path, out_dir: Path) -> dict:
    """Pack state_dir into out_dir/state/<group>.tar; return {group: index entry}."""
    rels = walk_files(state_dir)
    by_group = {g: [] for g in GROUPS}
    for rel in rels:
        by_group[group_of(rel)].append((rel, state_dir / rel))
    index = {}
    for g in GROUPS:
        entry = write_tar(out_dir / "state" / f"{g}.tar", by_group[g])
        entry["path"] = f"state/{g}.tar"
        index[g] = entry
        print(f"  packed {entry['path']}: {entry['files']} files, {entry['bytes'] / 1e6:.1f} MB, "
              f"sha256 {entry['sha256'][:12]}", flush=True)
    return index


def master_digest(master: Path) -> tuple[int, str]:
    """(number of *.txt game files, digest over their names + contents)."""
    h = hashlib.sha256()
    n = 0
    for p in sorted(master.glob("*.txt")):
        h.update(p.name.encode("utf-8", "surrogateescape") + b"\0")
        h.update(hashlib.sha1(p.read_bytes()).digest())
        n += 1
    return n, h.hexdigest()


def count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            n += chunk.count(b"\n")
    return n


def count_representatives(jsonl: Path) -> int:
    n = 0
    with open(jsonl, "rb") as f:
        for line in f:
            if b'"is_dedup_representative": true' in line:
                n += 1
    return n


def write_state_json(dest: Path, index: dict, master: Path, note: str) -> dict:
    n_games, digest = master_digest(master)
    meta = {
        "format": 1,
        "updated_utc": utcnow(),
        "updated_by": note,
        "master_games": n_games,
        "master_digest": digest,
        "tars": index,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _extract(tar_path: Path, dest: Path, expect_files: int) -> None:
    n = 0
    with tarfile.open(tar_path, mode="r:") as tf:
        for m in tf:
            if not m.isfile() or not (m.name.startswith("master/") or m.name.startswith("sources/")):
                raise SystemExit(f"unexpected member {m.name!r} in {tar_path}")
            tf.extract(m, dest, filter="data")
            n += 1
    if n != expect_files:
        raise SystemExit(f"{tar_path}: extracted {n} files, index says {expect_files}")


def cmd_fetch(args) -> None:
    state_dir, work = Path(args.state_dir), Path(args.work_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise SystemExit(f"state dir {state_dir} is not empty; refusing to overwrite it")
    state_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    revision = None
    if args.from_dir:
        src = Path(args.from_dir)
    else:
        from huggingface_hub import HfApi, snapshot_download
        api = HfApi()
        info = api.dataset_info(args.repo)
        revision = info.sha
        files = api.list_repo_files(args.repo, repo_type="dataset", revision=revision)
        if "state/STATE.json" not in files:
            raise SystemExit(f"{args.repo}@{revision[:8]} has no state/STATE.json; bootstrap it first")
        src = work / "hf_snapshot"
        # Everything except .gitattributes: the state tarballs and the current dataset files.
        snapshot_download(args.repo, repo_type="dataset", revision=revision, local_dir=src,
                          ignore_patterns=[".gitattributes"])
        print(f"downloaded {args.repo}@{revision[:8]} ({len(files) - 1} files)", flush=True)

    meta = json.loads((src / "state" / "STATE.json").read_text())
    for g in GROUPS:
        entry = meta["tars"][g]
        tar_path = src / entry["path"]
        got = sha256_file(tar_path)
        if got != entry["sha256"]:
            raise SystemExit(f"{tar_path}: sha256 {got} != {entry['sha256']} in STATE.json")
        _extract(tar_path, state_dir, entry["files"])
        print(f"  unpacked {entry['path']}: {entry['files']} files", flush=True)
        if not args.from_dir:
            tar_path.unlink()

    # Keep the currently published dataset files for the shrink guard / comparison.
    published = work / "published"
    published_rows = None
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        if rel.startswith("state/") or rel.startswith(".cache/") or rel == ".gitattributes":
            continue
        dest = published / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.from_dir:
            shutil.copy2(f, dest)
        else:
            shutil.move(f, dest)
    jsonl = published / "data" / "puzzlescript_games.jsonl"
    if jsonl.is_file():
        published_rows = count_lines(jsonl)

    n_games, digest = master_digest(state_dir / "master")
    if n_games != meta.get("master_games") or digest != meta.get("master_digest"):
        raise SystemExit(f"unpacked master ({n_games} games, {digest[:12]}) does not match "
                         f"STATE.json ({meta.get('master_games')} games, {str(meta.get('master_digest'))[:12]})")
    baseline = {
        "repo": args.repo,
        "hf_revision": revision,
        "tars": {g: meta["tars"][g]["sha256"] for g in GROUPS},
        "master_games": n_games,
        "master_digest": digest,
        "published_rows": published_rows,
        "state_updated_utc": meta.get("updated_utc"),
    }
    (work / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    if not args.from_dir:
        shutil.rmtree(src, ignore_errors=True)
    print(f"state ready at {state_dir}: {n_games} games; published dataset rows: {published_rows}")


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def cmd_publish(args) -> int:
    state_dir, work = Path(args.state_dir), Path(args.work_dir)
    summary_path = work / "summary.json"
    summary = {"published": "none"}

    def done(code=0):
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        return code

    baseline = json.loads((work / "baseline.json").read_text())
    master = state_dir / "master"
    n_games, digest = master_digest(master)
    summary.update(master_games=n_games, master_games_before=baseline["master_games"])

    out_dir = work / "state_out"
    shutil.rmtree(out_dir, ignore_errors=True)
    print("packing state ...", flush=True)
    index = pack(state_dir, out_dir)
    changed = [g for g in GROUPS if index[g]["sha256"] != baseline["tars"].get(g)]
    stage = work / "hf_stage"
    jsonl = stage / "data" / "puzzlescript_games.jsonl"
    dataset_built = jsonl.is_file()
    summary["state_groups_changed"] = changed
    if dataset_built:
        rows = count_lines(jsonl)
        summary.update(dataset_rows=rows, dedup_representatives=count_representatives(jsonl))
        ps_plus = work / "ps_plus_remove.txt"
        if ps_plus.is_file():
            summary["excluded_ps_plus"] = sum(1 for l in ps_plus.read_text().splitlines() if l.strip())

    if not changed and not dataset_built:
        print("nothing changed (state tarballs identical, no dataset rebuild); nothing to upload")
        return done()
    if digest != baseline["master_digest"] and not dataset_built:
        # State and dataset are committed together; publishing a changed corpus without
        # its dataset would leave the dataset behind until the corpus changes again.
        print("::error::the corpus changed but no dataset was built (run run_pipeline.sh, which rebuilds it)")
        summary["published"] = "refused (corpus changed without a dataset rebuild)"
        return done(1)

    # Shrink guard: losing more than MAX_SHRINK of the corpus or of the published rows
    # means the state was lost or damaged; never publish that.
    if n_games < (1 - MAX_SHRINK) * baseline["master_games"]:
        print(f"::error::refusing to publish: master shrank from {baseline['master_games']} to {n_games} games")
        summary["published"] = "refused (master shrank)"
        return done(1)
    old_rows = baseline.get("published_rows")
    if dataset_built and old_rows and summary["dataset_rows"] < (1 - MAX_SHRINK) * old_rows:
        print(f"::error::refusing to publish: dataset shrank from {old_rows} to {summary['dataset_rows']} rows")
        summary["published"] = "refused (dataset shrank)"
        return done(1)

    note = args.note or "ps_scraper.state publish"
    meta = write_state_json(out_dir / "state" / "STATE.json", index, master, note)

    from huggingface_hub import CommitOperationAdd
    ops = []
    if dataset_built:
        for f in sorted(p for p in stage.rglob("*") if p.is_file()):
            ops.append(CommitOperationAdd(path_in_repo=f.relative_to(stage).as_posix(), path_or_fileobj=str(f)))
    for g in changed:
        ops.append(CommitOperationAdd(path_in_repo=index[g]["path"],
                                      path_or_fileobj=str(out_dir / index[g]["path"])))
    ops.append(CommitOperationAdd(path_in_repo="state/STATE.json",
                                  path_or_fileobj=str(out_dir / "state" / "STATE.json")))
    message = f"Daily refresh: {n_games} games" if dataset_built else f"Update scraper state ({n_games} games)"
    plan = [op.path_in_repo for op in ops]
    print(f"commit plan ({message!r}):")
    for p in plan:
        print(f"  {p}")

    if args.dry_run:
        print("dry run: skipping the Hugging Face upload")
        summary["published"] = "skipped (dry run)"
        summary["would_upload"] = plan
        return done()
    token = os.environ.get("HF_TOKEN") or None
    if not token and not args.allow_cached_token:
        print("::warning::HF_TOKEN is not set; skipping the Hugging Face upload "
              "(add an HF_TOKEN secret with write access to the dataset)")
        summary["published"] = "skipped (no HF_TOKEN)"
        summary["would_upload"] = plan
        return done()

    from huggingface_hub import HfApi
    info = HfApi(token=token).create_commit(
        repo_id=args.repo, repo_type="dataset", operations=ops, commit_message=message,
        commit_description=f"{note}\nmaster: {baseline['master_games']} -> {n_games} games; "
                           f"state groups changed: {', '.join(changed) or 'none'}")
    print(f"published: {info.commit_url}")
    summary.update(published="dataset+state" if dataset_built else "state",
                   hf_commit=info.commit_url, uploaded=plan, state_meta=meta)
    return done()


# --------------------------------------------------------------------------- #
# bootstrap from the legacy script-doctor layout (host 209)
# --------------------------------------------------------------------------- #
def legacy_layout(script_doctor: Path, master: Path) -> dict[str, Path]:
    """state-relative path -> legacy location."""
    sd = script_doctor
    return {
        "master": master,
        "sources/lavelle": sd / "puzzlescript-analysis" / "raw_data" / "PuzzleScript",
        "sources/gist_trawl": sd / "puzzlescript-analysis" / "raw_data" / "gists_trawl",
        "sources/gist_staging": sd / "data" / "ps_dataset_staging",
        "sources/itch": sd / "data" / "scraped_games_itchio",
        "sources/increpare": sd / "data" / "scraped_games_increpare",
        "sources/pedro_archive": sd / "data" / "scraped_games",
        "sources/gallery": sd / "PuzzleScript" / "src" / "demo",
        "sources/games_dat.js": sd / "PuzzleScript" / "src" / "games_dat.js",
    }


def cmd_bootstrap(args) -> None:
    out = Path(args.out_dir)
    layout = legacy_layout(Path(args.script_doctor), Path(args.legacy_master))
    members = {g: [] for g in GROUPS}
    for dest, src in layout.items():
        if src.is_file():
            pairs = [(dest, src)]
        else:
            # master/_misfits.txt is a diagnostic list written by the legacy consolidate;
            # as a *.txt it was wrongly treated as a game, so it is not carried over.
            skip = (lambda rel: rel == "_misfits.txt") if dest == "master" else None
            pairs = [(f"{dest}/{rel}", src / rel) for rel in walk_files(src, skip=skip)]
        for arc, path in pairs:
            members[group_of(arc)].append((arc, path))
        print(f"  {dest:24} <- {src} ({len(pairs)} files)", flush=True)
    index = {}
    for g in GROUPS:
        entry = write_tar(out / "state" / f"{g}.tar", members[g])
        entry["path"] = f"state/{g}.tar"
        index[g] = entry
        print(f"  packed {entry['path']}: {entry['files']} files, {entry['bytes'] / 1e6:.1f} MB, "
              f"sha256 {entry['sha256'][:12]}", flush=True)
    # STATE.json needs the master digest; compute it from the legacy master minus _misfits.txt.
    h = hashlib.sha256()
    n = 0
    for p in sorted(Path(args.legacy_master).glob("*.txt")):
        if p.name == "_misfits.txt":
            continue
        h.update(p.name.encode("utf-8", "surrogateescape") + b"\0")
        h.update(hashlib.sha1(p.read_bytes()).digest())
        n += 1
    meta = {"format": 1, "updated_utc": utcnow(), "updated_by": args.note, "master_games": n,
            "master_digest": h.hexdigest(), "tars": index}
    (out / "state" / "STATE.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"bootstrap state written to {out / 'state'} ({n} games)")


def cmd_upload_bootstrap(args) -> None:
    from huggingface_hub import CommitOperationAdd, HfApi
    src = Path(args.dir) / "state"
    meta = json.loads((src / "STATE.json").read_text())
    for g in GROUPS:
        got = sha256_file(Path(args.dir) / meta["tars"][g]["path"])
        assert got == meta["tars"][g]["sha256"], g
    ops = [CommitOperationAdd(path_in_repo=meta["tars"][g]["path"],
                              path_or_fileobj=str(Path(args.dir) / meta["tars"][g]["path"])) for g in GROUPS]
    ops.append(CommitOperationAdd(path_in_repo="state/STATE.json", path_or_fileobj=str(src / "STATE.json")))
    api = HfApi()
    files = api.list_repo_files(args.repo, repo_type="dataset")
    if "state/STATE.json" in files and not args.force:
        raise SystemExit("state/STATE.json already exists in the dataset repo; pass --force to replace it")
    info = api.create_commit(repo_id=args.repo, repo_type="dataset", operations=ops,
                             commit_message=args.message,
                             commit_description=meta["updated_by"])
    print(f"uploaded bootstrap state: {info.commit_url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=HF_REPO)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--state-dir", default=str(paths.STATE_DIR))
    f.add_argument("--work-dir", default=str(paths.WORK_DIR))
    f.add_argument("--from-dir", default=None, help="Use a local folder holding state/ (+ dataset files) instead of the Hub")

    d = sub.add_parser("digest")
    d.add_argument("--master", default=str(paths.MASTER_DIR))

    p = sub.add_parser("publish")
    p.add_argument("--state-dir", default=str(paths.STATE_DIR))
    p.add_argument("--work-dir", default=str(paths.WORK_DIR))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--note", default=None, help="Recorded in STATE.json and the commit description")
    p.add_argument("--allow-cached-token", action="store_true",
                   help="Upload even without $HF_TOKEN, using a locally cached Hugging Face login")

    b = sub.add_parser("bootstrap")
    b.add_argument("--script-doctor", required=True)
    b.add_argument("--legacy-master", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--note", default="bootstrap from the script-doctor layout")

    u = sub.add_parser("upload-bootstrap")
    u.add_argument("--dir", required=True)
    u.add_argument("--message", default="Add scraper state (state/) for the GitHub Actions pipeline")
    u.add_argument("--force", action="store_true")

    args = ap.parse_args()
    if args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "digest":
        n, h = master_digest(Path(args.master))
        print(n, h)
    elif args.cmd == "publish":
        return cmd_publish(args)
    elif args.cmd == "bootstrap":
        cmd_bootstrap(args)
    elif args.cmd == "upload-bootstrap":
        cmd_upload_bootstrap(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
