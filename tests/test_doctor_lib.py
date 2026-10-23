from __future__ import annotations

import json

from conftest import (
    ARTIFACT_TOOL,
    BASH_TOOL,
    EXA_TOOL,
    snapshot_line,
    title_line,
    tool_result_line,
    tool_use_line,
    user_line,
)
from doctor_lib import (
    artifact_disabled,
    audit_session,
    cleanup_period_days,
    find_session,
    is_claude_process_line,
    iter_tool_lists,
    lint_re2,
    read_session,
    tool_call_report,
)


def test_live_session_matcher_ignores_doctor_flags():
    session_id = "22222222-2222-2222-2222-222222222222"
    assert is_claude_process_line(
        f"/Users/example/.local/bin/claude --resume {session_id}", session_id
    )
    assert is_claude_process_line(
        f"ghostty -e /bin/zsh -lic claude --resume {session_id}", session_id
    )
    assert not is_claude_process_line(
        f"python3 scripts/doctor_fix.py --session {session_id} --claude-home /tmp",
        session_id,
    )


def test_read_session_roundtrip(make_session):
    _home, _session_id, path = make_session(
        [title_line("demo", "sid"), user_line("hello", "sid")]
    )
    session_file = read_session(path)
    assert len(session_file.lines) == 2
    assert session_file.lines[1].obj["message"]["content"][0]["text"] == "hello"
    assert b"\n".join(line.raw for line in session_file.lines) == path.read_bytes().rstrip(b"\n")
    assert session_file.ends_with_newline is True


def test_read_session_without_trailing_newline(make_session):
    _home, _session_id, path = make_session([user_line("hi", "sid")], trailing_newline=False)
    session_file = read_session(path)
    assert session_file.ends_with_newline is False
    assert len(session_file.lines) == 1


def test_lint_re2_flags_unsupported_features():
    assert "lookahead" in lint_re2(r"^(?!__.*__$)")
    assert "lookbehind" in lint_re2(r"(?<=x)y")
    assert "backreference" in lint_re2(r"(a)\1")
    assert "atomic-group" in lint_re2(r"(?>a)")
    assert lint_re2(r"^[a-z]+(?: [a-z]+)*$") == []
    assert lint_re2(r"^\p{Cc}{1,3}$") == []


def test_iter_tool_lists_only_reads_prompt_snapshots(make_session):
    other = {
        "type": "attachment",
        "uuid": "att-2",
        "timestamp": "2026-10-23T00:00:00.000Z",
        "attachment": {"type": "file", "tools": [ARTIFACT_TOOL]},
    }
    _home, _session_id, path = make_session(
        [snapshot_line([BASH_TOOL], "sid"), other]
    )
    session_file = read_session(path)
    lists = list(iter_tool_lists(session_file.lines))
    assert len(lists) == 1
    assert [tool["name"] for tool in lists[0][2]] == ["Bash"]


def test_tool_call_report_detects_dangling_and_orphan(make_session):
    _home, _session_id, path = make_session(
        [
            tool_use_line("toolu_1", "Bash", "sid", uuid="a1"),
            tool_use_line("toolu_2", "Read", "sid", uuid="a2"),
            tool_result_line("toolu_1", "sid", uuid="r1"),
            tool_result_line("toolu_missing", "sid", uuid="r2"),
        ]
    )
    session_file = read_session(path)
    dangling, orphans = tool_call_report(session_file.lines)
    assert [item[1] for item in dangling] == ["toolu_2"]
    assert [item[1] for item in orphans] == ["toolu_missing"]


def test_artifact_disabled_reads_all_known_switches(make_session, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_ARTIFACT", raising=False)
    home, _, _ = make_session([], settings={"env": {"CLAUDE_CODE_DISABLE_ARTIFACT": "1"}})
    assert artifact_disabled(home) is True

    home, _, _ = make_session([], settings={"enableArtifact": False})
    assert artifact_disabled(home) is True

    home, _, _ = make_session([], settings={"disableArtifact": True})
    assert artifact_disabled(home) is True

    home, _, _ = make_session([], settings={})
    assert artifact_disabled(home) is False


def test_audit_flags_artifact_and_regex(make_session):
    home, session_id, _ = make_session(
        [snapshot_line([BASH_TOOL, ARTIFACT_TOOL, EXA_TOOL], "sid")],
        settings={"env": {"CLAUDE_CODE_DISABLE_ARTIFACT": "1"}},
    )
    report = audit_session(home, find_session(home, session_id))
    codes = {finding.code for finding in report.findings}
    assert "artifact-in-snapshot" in codes
    assert "re2-incompatible-schema" in codes
    assert report.worst_severity() == "error"


def test_audit_does_not_flag_artifact_when_enabled(make_session):
    home, session_id, _ = make_session([snapshot_line([BASH_TOOL], "sid")])
    report = audit_session(home, find_session(home, session_id))
    assert "artifact-in-snapshot" not in {finding.code for finding in report.findings}
    json.dumps(report.stats)  # stats stay serializable


def test_cleanup_period_days_applies_the_default(make_session):
    home, _, _ = make_session([], settings={"cleanupPeriodDays": 3650})
    assert cleanup_period_days(home) == 3650

    home, _, _ = make_session([], settings={})
    assert cleanup_period_days(home) == 30

    home, _, _ = make_session([], settings={"cleanupPeriodDays": 0})
    assert cleanup_period_days(home) == 30
