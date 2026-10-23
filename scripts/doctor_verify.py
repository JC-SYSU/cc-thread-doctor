#!/usr/bin/env python3
"""Verify a repaired session, statically or with a real shadow resume.

Static mode re-reads the file and runs the same checks as ``doctor_audit.py``.
Shadow mode copies the session to a fresh session id, resumes the copy with the
real ``claude`` CLI and a one-word prompt, then deletes the copy. It proves the
file is resumable on this machine with the configured provider; it cannot
promise the remote service accepts the transcript forever.

Shadow resume spends tokens. The estimate is printed first; runs above
--max-estimated-tokens require --allow-cost.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from doctor_lib import (
    audit_session,
    claude_home,
    estimate_request_tokens,
    find_session,
    fresh_session_id,
    read_session,
    session_cwd,
    session_is_live,
)

SHADOW_PROMPT = "Reply with exactly: ok"


def _print_static(report) -> bool:
    if not report.findings:
        print("static: OK, no findings")
        return True
    for finding in report.findings:
        where = f" line {finding.line}" if finding.line is not None else ""
        print(f"static: {finding.severity.upper()} {finding.code}{where}: {finding.message}")
    return report.worst_severity() != "error"


def _parse_cli_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "type" in payload:
            return payload
    return None


def _cleanup_shadow(home: Path, new_id: str, project_dir: str) -> list[Path]:
    removed: list[Path] = []
    candidates = [
        home / "projects" / project_dir / f"{new_id}.jsonl",
        home / "session-env" / new_id,
        home / "file-history" / new_id,
    ]
    backups = home / "backups"
    if backups.is_dir():
        candidates.extend(backups.rglob(f"{new_id}.jsonl"))
    for path in candidates:
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(path)
            elif path.exists():
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def cmd_shadow(args, home: Path, report) -> int:
    session = report.session
    if session_is_live(session.session_id):
        print(
            "warning: the original session is open in a running claude process; "
            "the shadow copy is independent, but close the original before any fix",
            file=sys.stderr,
        )

    source = read_session(session.path)
    estimate = estimate_request_tokens(source)
    print(f"shadow: estimated input tokens ~{estimate:,}")
    if estimate > args.max_estimated_tokens and not args.allow_cost:
        print(
            f"error: estimate exceeds --max-estimated-tokens ({args.max_estimated_tokens:,}). "
            "Re-run with --allow-cost to accept the spend.",
            file=sys.stderr,
        )
        return 1

    new_id = fresh_session_id()
    original_id_bytes = session.session_id.encode()
    data = session.path.read_bytes()
    if original_id_bytes not in data:
        print("error: session id not found in the file; cannot build a copy", file=sys.stderr)
        return 2
    copy_path = session.path.with_name(f"{new_id}.jsonl")
    copy_path.write_bytes(data.replace(original_id_bytes, new_id.encode()))

    claude_bin = args.claude_bin or os.environ.get("CC_THREAD_DOCTOR_CLAUDE_BIN") or "claude"
    # Resume resolves a session id in the context of a working directory;
    # run the copy from the session's own recorded cwd when it still exists.
    cwd = session_cwd(source.lines)
    if not cwd or not os.path.isdir(cwd):
        cwd = str(Path.home())
    command = [
        claude_bin,
        "--resume",
        new_id,
        "-p",
        SHADOW_PROMPT,
        "--output-format",
        "json",
    ]
    if args.model:
        command.extend(["--model", args.model])

    print(f"shadow: resuming copy {new_id[:8]} with {claude_bin!r} (hooks run as usual)")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=args.timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        print(f"error: {claude_bin!r} not found on PATH", file=sys.stderr)
        _cleanup_shadow(home, new_id, session.project_dir)
        return 2
    except subprocess.TimeoutExpired:
        print(f"error: shadow resume timed out after {args.timeout}s", file=sys.stderr)
        _cleanup_shadow(home, new_id, session.project_dir)
        return 2

    payload = _parse_cli_json(completed.stdout)
    ok = bool(
        payload
        and payload.get("is_error") is False
        and payload.get("subtype") == "success"
    )

    if payload:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        print(f"shadow: result={payload.get('result')!r} is_error={payload.get('is_error')}")
        if usage:
            print(
                "shadow: usage input_tokens={:,} output_tokens={:,}".format(
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                )
            )
        if payload.get("total_cost_usd") is not None:
            print(
                "shadow: reported total_cost_usd=${:.4f} "
                "(may include the session's recorded history)".format(
                    float(payload["total_cost_usd"])
                )
            )
    else:
        print("shadow: no JSON result parsed from the CLI output", file=sys.stderr)
        if completed.stderr.strip():
            print(completed.stderr.strip()[-2000:], file=sys.stderr)

    removed = _cleanup_shadow(home, new_id, session.project_dir)
    print(f"shadow: cleaned up {len(removed)} path(s)")

    if ok:
        print("shadow: PASS, the repaired session resumed successfully")
        return 0
    print("shadow: FAIL, the copy did not resume successfully", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="session id or unambiguous prefix")
    parser.add_argument("--shadow-resume", action="store_true", help="resume a copy with the real CLI")
    parser.add_argument("--allow-cost", action="store_true", help="accept the estimated token spend")
    parser.add_argument(
        "--max-estimated-tokens",
        type=int,
        default=200_000,
        help="refuse a shadow run estimated above this (default: 200000)",
    )
    parser.add_argument("--timeout", type=int, default=900, help="shadow resume timeout in seconds")
    parser.add_argument("--claude-bin", help="path to the claude CLI (default: PATH)")
    parser.add_argument("--model", help="pass a model override to the shadow resume")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--claude-home", help="override ~/.claude (or set CTD_CLAUDE_HOME)")
    args = parser.parse_args(argv)

    home = claude_home(args.claude_home)
    try:
        session = find_session(home, args.session)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = audit_session(home, session)
    static_ok = _print_static(report)

    if args.json:
        print(
            json.dumps(
                {
                    "session_id": session.session_id,
                    "static_ok": static_ok,
                    "findings": [finding.code for finding in report.findings],
                },
                indent=2,
            )
        )

    if not args.shadow_resume:
        return 0 if static_ok else 1

    shadow_code = cmd_shadow(args, home, report)
    if shadow_code != 0:
        return shadow_code
    return 0 if static_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
