#!/usr/bin/env python3
"""Read-only health scan for Claude Code session files.

Reads ~/.claude/projects/**/*.jsonl and reports only machine-state problems:
invalid JSON, unsafe tool definitions replayed from a prompt snapshot,
unanswered tool calls, and runaway size. It never treats text that merely
mentions an old error as a defect.

Exit codes: 0 no errors, 1 at least one error finding (or a warning with
--strict), 2 the scan itself failed.
"""
from __future__ import annotations

import argparse
import json
import sys

from doctor_lib import (
    audit_session,
    claude_home,
    find_session,
    human_bytes,
    list_sessions,
)

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

FIX_HINTS = {
    "artifact-in-snapshot": "doctor_fix.py tools --session {id} --drop Artifact --apply",
    "re2-incompatible-schema": "doctor_fix.py tools --session {id} --drop <tool-name> --apply  (or use a provider that accepts the pattern)",
    "truncated-tail": "doctor_fix.py repair-tail --session {id} --apply",
    "dangling-tool-use": "not automated in v0.1; see references/failure-modes.md",
    "unanswered-final-turn": "expected while a turn runs; if the session was interrupted, see references/failure-modes.md",
    "unanswered-server-tool": "not automated in v0.1; see references/failure-modes.md",
    "corrupt-lines": "not automated in v0.1; restore an earlier backup or trim manually",
    "orphan-tool-result": "not automated in v0.1",
    "oversized-session": "planned: doctor_fix.py slim (v0.2)",
}


def _report_dict(report) -> dict:
    return {
        "session_id": report.session.session_id,
        "project_dir": report.session.project_dir,
        "path": str(report.session.path),
        "title": report.title,
        "size": report.session.size,
        "size_human": human_bytes(report.session.size),
        "worst_severity": report.worst_severity(),
        "findings": [
            {
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "line": item.line,
                "detail": item.detail,
            }
            for item in report.findings
        ],
        "stats": {
            "lines": report.stats.get("lines"),
            "images": report.stats.get("images"),
            "image_base64_bytes": report.stats.get("image_base64_bytes"),
            "blob_bytes": report.stats.get("blob_bytes"),
            "largest_line_bytes": report.stats.get("largest_line_bytes"),
        },
    }


def _print_detail(report) -> None:
    session = report.session
    label = report.title or "(untitled)"
    print(
        f"{session.session_id[:8]}  {label}  "
        f"{human_bytes(session.size)}  {report.stats.get('lines', 0)} lines"
    )
    if not report.findings:
        print("  OK  no findings")
        return
    for finding in sorted(report.findings, key=lambda item: SEVERITY_ORDER.get(item.severity, 9)):
        where = f"  line {finding.line}" if finding.line is not None else ""
        print(f"  {finding.severity.upper():7} {finding.code}{where}")
        print(f"          {finding.message}")
        hint = FIX_HINTS.get(finding.code)
        if hint:
            print(f"          fix: {hint.format(id=session.session_id)}")


def _print_table(reports) -> None:
    width = max((len(report.session.session_id[:8]) for report in reports), default=8)
    for report in sorted(
        reports,
        key=lambda item: (
            SEVERITY_ORDER.get(item.worst_severity() or "info", 3),
            -item.session.mtime,
        ),
    ):
        worst = report.worst_severity()
        flag = {"error": "ERROR", "warning": "WARN ", "info": "INFO ", None: "OK   "}[worst]
        codes = ", ".join(item.code for item in report.findings) or "-"
        title = report.title or "(untitled)"
        print(
            f"{flag}  {report.session.session_id[:8]:<{width}}  "
            f"{human_bytes(report.session.size):>9}  {codes}  {title}"
        )
    errors = sum(1 for report in reports if report.worst_severity() == "error")
    warnings = sum(1 for report in reports if report.worst_severity() == "warning")
    print(f"\n{len(reports)} session(s), {errors} with errors, {warnings} with warnings")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="session id or unambiguous prefix")
    parser.add_argument("--all", action="store_true", help="scan every session (default)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    parser.add_argument("--claude-home", help="override ~/.claude (or set CTD_CLAUDE_HOME)")
    args = parser.parse_args(argv)

    home = claude_home(args.claude_home)

    try:
        if args.session:
            sessions = [find_session(home, args.session)]
        else:
            sessions = list_sessions(home)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not sessions:
        print(f"no sessions under {home / 'projects'}", file=sys.stderr)
        return 2

    reports = [audit_session(home, session) for session in sessions]

    if args.json:
        print(json.dumps([_report_dict(report) for report in reports], indent=2, ensure_ascii=False))
    elif len(reports) == 1:
        _print_detail(reports[0])
    else:
        _print_table(reports)

    has_error = any(report.worst_severity() == "error" for report in reports)
    has_warning = any(report.worst_severity() == "warning" for report in reports)
    if has_error or (args.strict and has_warning):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
