from __future__ import annotations

import json

from conftest import (
    ARTIFACT_TOOL,
    BASH_TOOL,
    EXA_TOOL,
    dump,
    snapshot_line,
    title_line,
    user_line,
)
from doctor_fix import main as fix_main
from doctor_lib import iter_tool_lists, read_session, sha256_file, tool_name


def _backup_dirs(home):
    root = home / ".cc-thread-doctor-backups"
    return sorted(root.glob("*")) if root.is_dir() else []


def test_tools_dry_run_does_not_write(make_session):
    home, session_id, path = make_session(
        [snapshot_line([BASH_TOOL, ARTIFACT_TOOL], "sid")]
    )
    before = path.read_bytes()
    code = fix_main(
        ["tools", "--session", session_id, "--drop", "Artifact", "--claude-home", str(home)]
    )
    assert code == 0
    assert path.read_bytes() == before
    assert _backup_dirs(home) == []


def test_tools_apply_removes_only_the_named_tool(make_session):
    home, session_id, path = make_session(
        [
            snapshot_line([BASH_TOOL, ARTIFACT_TOOL, EXA_TOOL], "sid", uuid="snap-1"),
            user_line("hello", "sid"),
            title_line("demo", "sid"),
            snapshot_line([ARTIFACT_TOOL, BASH_TOOL], "sid", uuid="snap-2"),
        ]
    )
    before_lines = path.read_bytes().split(b"\n")
    before_hash = sha256_file(path)

    code = fix_main(
        [
            "tools",
            "--session",
            session_id,
            "--drop",
            "Artifact",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0

    after_lines = path.read_bytes().split(b"\n")
    # Lines 1 and 2 have no snapshot; they must keep their exact bytes.
    assert after_lines[1] == before_lines[1]
    assert after_lines[2] == before_lines[2]
    assert after_lines[0] != before_lines[0]
    assert after_lines[3] != before_lines[3]

    session_file = read_session(path)
    names = [
        tool_name(tool)
        for _, _, tools in iter_tool_lists(session_file.lines)
        for tool in tools
    ]
    assert "Artifact" not in names
    assert "Bash" in names and "mcp__exa__search" in names

    backups = _backup_dirs(home)
    assert len(backups) == 1
    manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == before_hash
    assert sha256_file(backups[0] / "session.jsonl") == before_hash


def test_tools_expect_count_mismatch_writes_nothing(make_session):
    home, session_id, path = make_session(
        [snapshot_line([BASH_TOOL, ARTIFACT_TOOL], "sid")]
    )
    before = path.read_bytes()
    code = fix_main(
        [
            "tools",
            "--session",
            session_id,
            "--drop",
            "Artifact",
            "--expect-count",
            "5",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 1
    assert path.read_bytes() == before
    assert _backup_dirs(home) == []


def test_repair_tail_drops_only_the_broken_suffix(make_session):
    home, session_id, path = make_session([user_line("hi", "sid")])
    with open(path, "ab") as handle:
        handle.write(b'{"type":"assistant","message":')
    before = path.read_bytes()

    code = fix_main(
        ["repair-tail", "--session", session_id, "--apply", "--claude-home", str(home)]
    )
    assert code == 0

    session_file = read_session(path)
    assert len(session_file.lines) == 1
    assert all(line.error is None for line in session_file.lines)
    assert session_file.lines[0].raw == before.split(b"\n")[0]
    assert len(_backup_dirs(home)) == 1


def test_repair_tail_refuses_middle_corruption(make_session):
    home, session_id, path = make_session([])
    body = "\n".join(
        [
            dump(user_line("hi", "sid")),
            '{"broken":',
            dump(title_line("demo", "sid")),
        ]
    ) + "\n"
    path.write_text(body, encoding="utf-8")
    before = path.read_bytes()

    code = fix_main(
        ["repair-tail", "--session", session_id, "--apply", "--claude-home", str(home)]
    )
    assert code == 1
    assert path.read_bytes() == before
    assert _backup_dirs(home) == []


def test_restore_puts_the_backup_back(make_session):
    home, session_id, path = make_session(
        [snapshot_line([BASH_TOOL, ARTIFACT_TOOL], "sid")]
    )
    original = path.read_bytes()
    code = fix_main(
        [
            "tools",
            "--session",
            session_id,
            "--drop",
            "Artifact",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0
    backup_dir = _backup_dirs(home)[0]

    target = home / "restored.jsonl"
    code = fix_main(["restore", "--backup", str(backup_dir), "--target", str(target)])
    assert code == 0
    assert target.read_bytes() == original
