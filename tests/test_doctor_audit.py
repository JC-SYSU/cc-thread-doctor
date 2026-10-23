from __future__ import annotations

import json

from conftest import (
    ARTIFACT_TOOL,
    BASH_TOOL,
    snapshot_line,
    title_line,
    tool_use_line,
    user_line,
)
from doctor_audit import main as audit_main
from doctor_lib import audit_session, find_session


def test_truncated_tail(make_session):
    home, session_id, path = make_session([user_line("hi", "sid")])
    with open(path, "ab") as handle:
        handle.write(b'{"type":"assistant","message":')
    report = audit_session(home, find_session(home, session_id))
    assert "truncated-tail" in {finding.code for finding in report.findings}


def test_dangling_tool_use(make_session):
    home, session_id, _ = make_session(
        [
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
            user_line("continue", "sid", uuid="u2"),
        ]
    )
    report = audit_session(home, find_session(home, session_id))
    finding = next(item for item in report.findings if item.code == "dangling-tool-use")
    assert "Bash@0" in finding.message
    assert finding.severity == "error"


def test_unanswered_final_turn_is_a_warning(make_session):
    home, session_id, _ = make_session(
        [user_line("hi", "sid"), tool_use_line("toolu_1", "Bash", "sid", uuid="a2")]
    )
    report = audit_session(home, find_session(home, session_id))
    finding = next(
        item for item in report.findings if item.code == "unanswered-final-turn"
    )
    assert finding.severity == "warning"
    assert report.worst_severity() == "warning"


def test_cli_json_reports_and_exit_code(make_session, capsys):
    home, session_id, _ = make_session(
        [snapshot_line([BASH_TOOL, ARTIFACT_TOOL], "sid")],
        settings={"env": {"CLAUDE_CODE_DISABLE_ARTIFACT": "1"}},
    )
    code = audit_main(
        ["--session", session_id, "--json", "--claude-home", str(home)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload[0]["session_id"] == session_id
    assert "artifact-in-snapshot" in {item["code"] for item in payload[0]["findings"]}


def test_cli_clean_session_exits_zero(make_session):
    home, session_id, _ = make_session([snapshot_line([BASH_TOOL], "sid")])
    code = audit_main(["--session", session_id, "--claude-home", str(home)])
    assert code == 0


def test_cli_title_is_reported(make_session, capsys):
    home, session_id, _ = make_session(
        [title_line("macOS 27兼容性调研", "sid"), user_line("hi", "sid")]
    )
    audit_main(["--session", session_id, "--claude-home", str(home)])
    assert "macOS 27兼容性调研" in capsys.readouterr().out
