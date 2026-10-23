from __future__ import annotations

import json

from conftest import (
    ARTIFACT_TOOL,
    BASH_TOOL,
    EXA_TOOL,
    dump,
    snapshot_line,
    title_line,
    tool_use_line,
    user_line,
)
from doctor_fix import main as fix_main
from doctor_lib import (
    iter_tool_lists,
    read_session,
    sha256_file,
    split_unanswered_calls,
    tool_name,
)


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


def test_unanswered_calls_fills_the_next_user_message(make_session):
    home, session_id, path = make_session(
        [
            user_line("go", "sid", uuid="u1"),
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
            user_line("next prompt", "sid", uuid="u2"),
        ]
    )
    before = path.read_bytes().split(b"\n")
    code = fix_main(
        [
            "unanswered-calls",
            "--session",
            session_id,
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0

    after = read_session(path)
    assert len(after.lines) == 3
    content = after.lines[2].obj["message"]["content"]
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "toolu_1"
    assert content[0]["is_error"] is True
    assert content[1] == {"type": "text", "text": "next prompt"}
    assert after.lines[0].raw == before[0]
    assert after.lines[1].raw == before[1]
    lost, tail = split_unanswered_calls(after.lines)
    assert [item for item in lost + tail if not item[3]] == []
    assert len(_backup_dirs(home)) == 1


def test_unanswered_calls_appends_a_result_for_the_final_turn(make_session):
    home, session_id, path = make_session(
        [
            user_line("go", "sid", uuid="u1"),
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
        ]
    )
    before = path.read_bytes().split(b"\n")
    code = fix_main(
        [
            "unanswered-calls",
            "--session",
            session_id,
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0

    after = read_session(path)
    assert len(after.lines) == 3
    record = after.lines[2].obj
    assert record["type"] == "user"
    assert record["parentUuid"] == "a1"
    block = record["message"]["content"][0]
    assert block["tool_use_id"] == "toolu_1"
    assert block["is_error"] is True
    assert after.lines[0].raw == before[0]
    assert after.lines[1].raw == before[1]
    lost, tail = split_unanswered_calls(after.lines)
    assert [item for item in lost + tail if not item[3]] == []


def test_unanswered_calls_expect_count_guard(make_session):
    home, session_id, path = make_session(
        [
            user_line("go", "sid", uuid="u1"),
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
            user_line("next", "sid", uuid="u2"),
        ]
    )
    before = path.read_bytes()
    code = fix_main(
        [
            "unanswered-calls",
            "--session",
            session_id,
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


def test_unanswered_calls_ignores_server_tools(make_session):
    server_tool = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "2026-10-23T00:00:00.000Z",
        "sessionId": "sid",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "call_1", "name": "web_search"}
            ],
        },
    }
    home, session_id, path = make_session(
        [user_line("go", "sid", uuid="u1"), server_tool]
    )
    before = path.read_bytes()
    code = fix_main(
        [
            "unanswered-calls",
            "--session",
            session_id,
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0
    assert path.read_bytes() == before


def test_unanswered_calls_is_idempotent(make_session, capsys):
    home, session_id, path = make_session(
        [
            user_line("go", "sid", uuid="u1"),
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
            user_line("next", "sid", uuid="u2"),
        ]
    )
    first = fix_main(
        ["unanswered-calls", "--session", session_id, "--apply", "--claude-home", str(home)]
    )
    assert first == 0
    after_first = path.read_bytes()
    second = fix_main(
        ["unanswered-calls", "--session", session_id, "--apply", "--claude-home", str(home)]
    )
    assert second == 0
    assert path.read_bytes() == after_first
    assert "no unanswered client tool calls" in capsys.readouterr().out


def _image_block(size: int = 5000) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "A" * size,
        },
    }


def test_slim_replaces_images(make_session):
    home, session_id, path = make_session(
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "sessionId": "sid",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        _image_block(),
                    ],
                },
            },
            user_line("after", "sid", uuid="u2"),
        ]
    )
    before = path.read_bytes().split(b"\n")
    code = fix_main(
        ["slim", "--session", session_id, "--images", "--apply", "--claude-home", str(home)]
    )
    assert code == 0

    after = read_session(path)
    assert len(after.lines) == 2
    content = after.lines[0].obj["message"]["content"]
    assert content[1]["type"] == "text"
    assert "removed image/png" in content[1]["text"]
    assert after.lines[1].raw == before[1]
    assert len(_backup_dirs(home)) == 1


def test_slim_truncates_tool_outputs(make_session):
    long_text = "x" * 5000
    home, session_id, path = make_session(
        [
            user_line("go", "sid", uuid="u1"),
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "sessionId": "sid",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": long_text,
                        }
                    ],
                },
            },
        ]
    )
    code = fix_main(
        [
            "slim",
            "--session",
            session_id,
            "--tool-outputs",
            "1000",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0

    after = read_session(path)
    result = after.lines[1].obj["message"]["content"][0]["content"]
    assert len(result) <= 1000
    assert result.startswith("x" * 800)
    assert "truncated 5,000 chars" in result


def test_slim_reaches_nested_blocks_and_tool_use_result(make_session):
    home, session_id, path = make_session(
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "sessionId": "sid",
                "toolUseResult": {"stdout": "y" * 3000, "ok": True},
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [
                                {"type": "text", "text": "z" * 3000},
                                _image_block(3000),
                            ],
                        }
                    ],
                },
            }
        ]
    )
    code = fix_main(
        [
            "slim",
            "--session",
            session_id,
            "--images",
            "--tool-outputs",
            "800",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 0

    after = read_session(path)
    obj = after.lines[0].obj
    content = obj["message"]["content"][0]["content"]
    assert len(content[0]["text"]) <= 800
    assert content[0]["text"].startswith("z" * 600)
    assert "truncated 3,000 chars" in content[0]["text"]
    assert content[1]["type"] == "text"
    assert "removed image/png" in content[1]["text"]
    assert len(obj["toolUseResult"]["stdout"]) <= 800
    assert obj["toolUseResult"]["stdout"].startswith("y" * 600)
    assert obj["toolUseResult"]["ok"] is True


def test_slim_expect_count_guard(make_session):
    home, session_id, path = make_session(
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "sessionId": "sid",
                "message": {
                    "role": "user",
                    "content": [_image_block()],
                },
            }
        ]
    )
    before = path.read_bytes()
    code = fix_main(
        [
            "slim",
            "--session",
            session_id,
            "--images",
            "--expect-count",
            "9",
            "--apply",
            "--claude-home",
            str(home),
        ]
    )
    assert code == 1
    assert path.read_bytes() == before
    assert _backup_dirs(home) == []


def test_slim_is_idempotent(make_session, capsys):
    home, session_id, path = make_session(
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "sessionId": "sid",
                "message": {"role": "user", "content": [_image_block()]},
            }
        ]
    )
    first = fix_main(
        ["slim", "--session", session_id, "--images", "--apply", "--claude-home", str(home)]
    )
    assert first == 0
    after_first = path.read_bytes()
    second = fix_main(
        ["slim", "--session", session_id, "--images", "--apply", "--claude-home", str(home)]
    )
    assert second == 0
    assert path.read_bytes() == after_first
    assert "nothing to slim" in capsys.readouterr().out


def test_slim_requires_a_mode(make_session):
    home, session_id, path = make_session([user_line("hi", "sid")])
    before = path.read_bytes()
    code = fix_main(["slim", "--session", session_id, "--claude-home", str(home)])
    assert code == 1
    assert path.read_bytes() == before
