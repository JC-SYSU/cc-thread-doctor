from __future__ import annotations

import json
import os

from conftest import dump, user_line
from doctor_backup import list_snapshots
from doctor_backup import main as backup_main


def _snapshot(home, *extra):
    return backup_main(["snapshot", *extra, "--claude-home", str(home)])


def test_snapshot_copies_files_and_writes_manifest(make_session):
    home, _session_id, path = make_session([user_line("hi", "sid")])
    assert _snapshot(home) == 0

    snapshots = list_snapshots(home)
    assert len(snapshots) == 1
    relative = path.relative_to(home / "projects")
    assert (snapshots[0] / relative).read_bytes() == path.read_bytes()
    manifest = json.loads((snapshots[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == 1
    assert manifest["copied"] == 1
    assert manifest["linked"] == 0


def test_second_snapshot_hardlinks_unchanged_files(make_session):
    home, _session_id, path = make_session([user_line("hi", "sid")])
    assert _snapshot(home) == 0
    assert _snapshot(home) == 0

    first, second = list_snapshots(home)
    relative = path.relative_to(home / "projects")
    assert os.stat(first / relative).st_ino == os.stat(second / relative).st_ino
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["linked"] == 1
    assert manifest["copied"] == 0


def test_changed_file_is_copied_not_linked(make_session):
    home, _session_id, path = make_session([user_line("hi", "sid")])
    assert _snapshot(home) == 0
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(dump(user_line("more", "sid", uuid="u2")) + "\n")
    assert _snapshot(home) == 0

    first, second = list_snapshots(home)
    relative = path.relative_to(home / "projects")
    assert os.stat(first / relative).st_ino != os.stat(second / relative).st_ino
    assert (second / relative).read_bytes() == path.read_bytes()


def test_retention_keeps_the_newest_snapshots(make_session):
    home, _session_id, _path = make_session([user_line("hi", "sid")])
    for _ in range(3):
        assert _snapshot(home, "--keep", "2") == 0
    assert len(list_snapshots(home)) == 2


def test_dry_run_creates_no_snapshot(make_session):
    home, _session_id, _path = make_session([user_line("hi", "sid")])
    assert _snapshot(home, "--dry-run") == 0
    assert list_snapshots(home) == []


def test_restore_puts_the_session_back(make_session):
    home, session_id, path = make_session([user_line("hi", "sid")])
    original = path.read_bytes()
    assert _snapshot(home) == 0
    path.write_bytes(b'{"broken":')

    code = backup_main(
        ["restore", "--session", session_id, "--claude-home", str(home)]
    )
    assert code == 0
    assert path.read_bytes() == original


def test_restore_missing_session_fails(make_session):
    home, _session_id, _path = make_session([user_line("hi", "sid")])
    assert _snapshot(home) == 0
    code = backup_main(
        ["restore", "--session", "doesnotexist", "--claude-home", str(home)]
    )
    assert code == 1
