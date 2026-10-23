#!/usr/bin/env python3
"""Snapshot Claude Code sessions and restore one file.

``snapshot`` mirrors ``~/.claude/projects`` into a timestamped directory under
``~/.claude/.cc-thread-doctor-backups/``. Files unchanged since the previous
snapshot are hardlinked, so snapshots after the first cost almost no disk
space. The source tree is only read.

Retention keeps the newest ``--keep`` snapshots (default 7).

``restore`` copies one session file back, verifies its SHA-256, and refuses to
overwrite a session that is open in a running ``claude`` process.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from doctor_lib import (
    __version__,
    claude_home,
    human_bytes,
    session_is_live,
    sha256_file,
)

SNAPSHOT_PREFIX = "projects-"
BACKUP_ROOT = ".cc-thread-doctor-backups"


def snapshot_root(home: Path) -> Path:
    return home / BACKUP_ROOT


def list_snapshots(home: Path) -> list[Path]:
    root = snapshot_root(home)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(SNAPSHOT_PREFIX)
    )


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def cmd_snapshot(args, home: Path) -> int:
    projects = home / "projects"
    if not projects.is_dir():
        print(f"error: {projects} does not exist", file=sys.stderr)
        return 1
    if args.keep < 1:
        print("error: --keep must be at least 1", file=sys.stderr)
        return 1

    files = list(_iter_files(projects))
    total_bytes = sum(path.stat().st_size for path in files)
    existing = list_snapshots(home)
    previous = existing[-1] if existing else None

    if args.dry_run:
        print(f"would snapshot {len(files)} file(s), {human_bytes(total_bytes)}")
        if previous:
            print(f"previous snapshot: {previous.name}")
        else:
            print("previous snapshot: none; the first snapshot copies every file")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = snapshot_root(home) / f"{SNAPSHOT_PREFIX}{stamp}"
    destination.mkdir(parents=True, exist_ok=False)

    copied = linked = 0
    for path in files:
        relative = path.relative_to(projects)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_stat = path.stat()
        previous_file = previous / relative if previous else None
        if previous_file and previous_file.is_file():
            previous_stat = previous_file.stat()
            if (
                previous_stat.st_size == source_stat.st_size
                and int(previous_stat.st_mtime) == int(source_stat.st_mtime)
            ):
                os.link(previous_file, target)
                linked += 1
                continue
        shutil.copy2(path, target)
        copied += 1

    manifest = {
        "tool": "cc-thread-doctor",
        "version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(projects),
        "files": len(files),
        "bytes": total_bytes,
        "copied": copied,
        "linked": linked,
        "previous": previous.name if previous else None,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    snapshots = list_snapshots(home)
    removed = []
    for old in snapshots[: -args.keep]:
        shutil.rmtree(old)
        removed.append(old.name)

    print(f"snapshot: {destination}")
    print(
        f"{len(files)} file(s), {human_bytes(total_bytes)} total, "
        f"{copied} copied, {linked} hardlinked"
    )
    if removed:
        print(f"pruned: {', '.join(removed)}")
    return 0


def cmd_list(_args, home: Path) -> int:
    snapshots = list_snapshots(home)
    if not snapshots:
        print("no snapshots")
        return 0
    for path in snapshots:
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            print(f"{path.name}  (no manifest)")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(
            f"{path.name}  {manifest.get('files', '?')} file(s)  "
            f"{human_bytes(int(manifest.get('bytes', 0)))}  "
            f"copied={manifest.get('copied', '?')} linked={manifest.get('linked', '?')}"
        )
    return 0


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".cc-thread-doctor-restore-", suffix=".tmp"
    )
    os.close(descriptor)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def cmd_restore(args, home: Path) -> int:
    snapshots = list_snapshots(home)
    if not snapshots:
        print("error: no snapshots", file=sys.stderr)
        return 1

    if args.snapshot in (None, "latest"):
        snapshot = snapshots[-1]
    else:
        snapshot = snapshot_root(home) / args.snapshot
        if not snapshot.is_dir():
            print(f"error: no snapshot named {args.snapshot!r}", file=sys.stderr)
            return 1

    exact = list(snapshot.rglob(f"{args.session}.jsonl"))
    matches = exact or list(snapshot.rglob(f"{args.session}*.jsonl"))
    if not matches:
        print(f"error: session {args.session!r} not in {snapshot.name}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        listing = "\n".join(f"  {path.relative_to(snapshot)}" for path in matches)
        print(f"error: ambiguous session id:\n{listing}", file=sys.stderr)
        return 1

    if session_is_live(args.session) and not args.force:
        print(
            "error: this session is open in a running claude process. "
            "Close it first, or pass --force.",
            file=sys.stderr,
        )
        return 1

    source = matches[0]
    relative = source.relative_to(snapshot)
    target = (
        Path(args.target).expanduser() if args.target else home / "projects" / relative
    )
    expected = sha256_file(source)
    _atomic_copy(source, target)
    actual = sha256_file(target)
    if actual != expected:
        print("error: restored file does not match the snapshot checksum", file=sys.stderr)
        return 2
    print(f"restored {target}")
    print(f"sha256: {actual}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="snapshot ~/.claude/projects")
    snapshot.add_argument("--keep", type=int, default=7)
    snapshot.add_argument("--dry-run", action="store_true")
    snapshot.add_argument("--claude-home")
    snapshot.set_defaults(func=cmd_snapshot)

    listing = subparsers.add_parser("list", help="list snapshots")
    listing.add_argument("--claude-home")
    listing.set_defaults(func=cmd_list)

    restore = subparsers.add_parser("restore", help="restore one session file")
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument("--session", required=True, help="session id or prefix")
    restore.add_argument("--target")
    restore.add_argument("--force", action="store_true")
    restore.add_argument("--claude-home")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    home = claude_home(getattr(args, "claude_home", None))
    return args.func(args, home)


if __name__ == "__main__":
    raise SystemExit(main())
