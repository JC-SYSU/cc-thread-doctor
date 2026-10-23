#!/usr/bin/env python3
"""Targeted repairs for Claude Code session files.

Every treatment is explicit, defaults to a dry run, and writes only after a
verified backup. Untouched lines are written back at their original bytes.

Commands:
  tools --session ID --drop NAME[,NAME]   remove tool definitions recorded in
                                          prompt snapshots (e.g. Artifact)
  repair-tail --session ID                drop trailing lines that are not JSON
  unanswered-calls --session ID           give interrupted tool calls a
                                          synthetic error result
  restore --backup DIR                    put a backup back in place
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from doctor_lib import (
    Line,
    SessionRef,
    assert_untouched,
    atomic_write_session,
    backup_session,
    claude_home,
    find_session,
    iter_tool_lists,
    read_session,
    session_is_live,
    sha256_file,
    split_unanswered_calls,
    tool_name,
)


def _refuse_live(session: SessionRef, force: bool) -> bool:
    if not force and session_is_live(session.session_id):
        print(
            "error: this session is open in a running claude process. "
            "Close it first, or pass --force if you are sure.",
            file=sys.stderr,
        )
        return True
    return False


def _atomic_restore(source: Path, target: Path) -> None:
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


def cmd_tools(args, home: Path) -> int:
    session = find_session(home, args.session)
    if _refuse_live(session, args.force):
        return 1

    drops = {name.strip() for name in args.drop.split(",") if name.strip()}
    if not drops:
        print("error: --drop needs at least one tool name", file=sys.stderr)
        return 1

    before = read_session(session.path)
    plans: list[tuple[int, str]] = []
    for line_index, _, tools in iter_tool_lists(before.lines):
        for tool in tools:
            name = tool_name(tool)
            if isinstance(name, str) and name in drops:
                plans.append((line_index, name))

    if not plans:
        print("no matching tool definitions in prompt snapshots; nothing to do")
        return 0

    for line_index, name in plans:
        print(f"line {line_index}: drop tool {name!r}")

    if args.expect_count is not None and len(plans) != args.expect_count:
        print(
            f"error: expected {args.expect_count} removal(s), found {len(plans)}; "
            "nothing written",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print(f"\ndry run: {len(plans)} removal(s). Re-run with --apply to write.")
        return 0

    working = read_session(session.path)
    if len(before.lines) != len(working.lines) or any(
        old.raw != new.raw for old, new in zip(before.lines, working.lines)
    ):
        print(
            "error: the session changed while the plan was being prepared; nothing written",
            file=sys.stderr,
        )
        return 1

    backup = backup_session(home, session, f"drop tools: {','.join(sorted(drops))}")
    before_hash = sha256_file(session.path)
    touched: set[int] = set()
    for line_index, _, tools in iter_tool_lists(working.lines):
        kept = [tool for tool in tools if tool_name(tool) not in drops]
        if len(kept) != len(tools):
            tools[:] = kept
            touched.add(line_index)
    for line in working.lines:
        if line.index in touched:
            line.raw = json.dumps(
                line.obj, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

    try:
        atomic_write_session(working, working.lines)
        after = read_session(session.path)
        assert_untouched(before, after, touched)
        remaining = [
            name
            for _, _, tools in iter_tool_lists(after.lines)
            for tool in tools
            if isinstance(name := tool_name(tool), str)
        ]
        leftover = sorted(set(remaining) & drops)
        if leftover:
            raise RuntimeError(f"tool(s) still present after the write: {leftover}")
    except Exception as exc:  # noqa: BLE001 - any failure must roll back
        _atomic_restore(backup / "session.jsonl", session.path)
        print(f"error: {exc}; original restored from {backup}", file=sys.stderr)
        return 2

    after_hash = sha256_file(session.path)
    print(f"backup: {backup}")
    print(f"sha256: {before_hash[:16]}… -> {after_hash[:16]}…")
    print(
        f"verified: {len(after.lines)} lines, {len(touched)} rewritten, "
        f"{len(after.lines) - len(touched)} untouched line(s) byte-identical"
    )
    return 0


def cmd_repair_tail(args, home: Path) -> int:
    session = find_session(home, args.session)
    if _refuse_live(session, args.force):
        return 1

    before = read_session(session.path)
    invalid = [line for line in before.lines if line.error]
    if not invalid:
        print("no invalid lines; nothing to do")
        return 0

    first_bad = invalid[0].index
    if not all(line.error for line in before.lines[first_bad:]):
        print(
            "error: invalid lines are not a trailing suffix; refusing to drop "
            "valid records that follow them",
            file=sys.stderr,
        )
        return 1

    count = len(before.lines) - first_bad
    print(f"drop {count} trailing line(s), starting at line {first_bad}")
    for line in before.lines[first_bad:]:
        print(f"  line {line.index}: {line.error}")

    if args.expect_count is not None and count != args.expect_count:
        print(
            f"error: expected {args.expect_count} line(s), found {count}; "
            "nothing written",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print(f"\ndry run: {count} line(s). Re-run with --apply to write.")
        return 0

    kept = before.lines[:first_bad]
    working = read_session(session.path)
    if len(before.lines) != len(working.lines) or any(
        old.raw != new.raw for old, new in zip(before.lines, working.lines)
    ):
        print(
            "error: the session changed while the plan was being prepared; nothing written",
            file=sys.stderr,
        )
        return 1

    backup = backup_session(home, session, "repair truncated tail")
    before_hash = sha256_file(session.path)

    try:
        atomic_write_session(working, working.lines[:first_bad])
        after = read_session(session.path)
        if len(after.lines) != len(kept):
            raise RuntimeError(f"line count changed: {len(kept)} -> {len(after.lines)}")
        if [line.raw for line in after.lines] != [line.raw for line in kept]:
            raise RuntimeError("kept lines changed bytes")
        if any(line.error for line in after.lines):
            raise RuntimeError("invalid lines remain after the repair")
    except Exception as exc:  # noqa: BLE001
        _atomic_restore(backup / "session.jsonl", session.path)
        print(f"error: {exc}; original restored from {backup}", file=sys.stderr)
        return 2

    after_hash = sha256_file(session.path)
    print(f"backup: {backup}")
    print(f"sha256: {before_hash[:16]}… -> {after_hash[:16]}…")
    print(f"verified: {len(after.lines)} lines, all valid JSON")
    return 0


def _synthetic_result(tool_id: str, text: str) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": text,
        "is_error": True,
    }


def _dump_line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def cmd_unanswered_calls(args, home: Path) -> int:
    session = find_session(home, args.session)
    if _refuse_live(session, args.force):
        return 1

    before = read_session(session.path)
    lost, tail = split_unanswered_calls(before.lines)
    lost = [item for item in lost if not item[3]]
    tail = [item for item in tail if not item[3]]

    plan: list[tuple[int, str, str | None, int | None]] = []
    for assistant_index, tool_id, name, _ in lost:
        target = next(
            (
                line.index
                for line in before.lines
                if line.index > assistant_index
                and line.obj
                and line.obj.get("type") == "user"
            ),
            None,
        )
        if target is None:
            print(
                f"warning: no user record after {tool_id}; leaving it untouched",
                file=sys.stderr,
            )
            continue
        plan.append((assistant_index, tool_id, name, target))
    for assistant_index, tool_id, name, _ in tail:
        plan.append((assistant_index, tool_id, name, None))

    if not plan:
        print("no unanswered client tool calls; nothing to do")
        return 0

    for assistant_index, tool_id, name, target in plan:
        if target is None:
            where = f"append a result after line {assistant_index}"
        else:
            where = f"insert a result into line {target}"
        print(f"line {assistant_index}: {name or '?'} ({tool_id}) -> {where}")

    if args.expect_count is not None and len(plan) != args.expect_count:
        print(
            f"error: expected {args.expect_count} call(s), found {len(plan)}; "
            "nothing written",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print(f"\ndry run: {len(plan)} call(s). Re-run with --apply to write.")
        return 0

    working = read_session(session.path)
    if len(before.lines) != len(working.lines) or any(
        old.raw != new.raw for old, new in zip(before.lines, working.lines)
    ):
        print(
            "error: the session changed while the plan was being prepared; nothing written",
            file=sys.stderr,
        )
        return 1

    backup = backup_session(home, session, "fill unanswered tool calls")
    before_hash = sha256_file(session.path)
    result_text = args.result_text or (
        "The tool did not return a result because the turn was interrupted."
    )

    by_target: dict[int, list[str]] = {}
    tail_groups: dict[int, list[str]] = {}
    for assistant_index, tool_id, _, target in plan:
        if target is None:
            tail_groups.setdefault(assistant_index, []).append(tool_id)
        else:
            by_target.setdefault(target, []).append(tool_id)

    touched: set[int] = set()
    appended: list[dict] = []
    try:
        for target, tool_ids in by_target.items():
            line = working.lines[target]
            if not line.obj or not isinstance(line.obj.get("message"), dict):
                raise RuntimeError(f"line {target} is not a message record")
            message = line.obj["message"]
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                raise TypeError(f"line {target} has no content list")
            message["content"] = [
                *(_synthetic_result(tool_id, result_text) for tool_id in tool_ids),
                *content,
            ]
            line.raw = _dump_line(line.obj)
            touched.add(target)

        for assistant_index, tool_ids in tail_groups.items():
            assistant_line = working.lines[assistant_index]
            if not assistant_line.obj:
                raise RuntimeError(f"line {assistant_index} is not a JSON record")
            assistant = assistant_line.obj
            record: dict = {}
            if isinstance(assistant.get("uuid"), str):
                record["parentUuid"] = assistant["uuid"]
            for key in (
                "isSidechain",
                "userType",
                "cwd",
                "sessionId",
                "session_id",
                "version",
                "gitBranch",
                "entrypoint",
            ):
                if key in assistant:
                    record[key] = assistant[key]
            record["type"] = "user"
            record["message"] = {
                "role": "user",
                "content": [
                    _synthetic_result(tool_id, result_text) for tool_id in tool_ids
                ],
            }
            record["uuid"] = str(uuid.uuid4())
            record["timestamp"] = datetime.now(UTC).isoformat()
            appended.append(record)

        for record in appended:
            working.lines.append(
                Line(index=len(working.lines), raw=_dump_line(record), obj=record)
            )

        atomic_write_session(working, working.lines)
        after = read_session(session.path)
        if len(after.lines) != len(before.lines) + len(appended):
            raise RuntimeError(
                f"line count changed: {len(before.lines)} -> {len(after.lines)} "
                f"(expected +{len(appended)})"
            )
        for index, old in enumerate(before.lines):
            if index in touched:
                continue
            if after.lines[index].raw != old.raw:
                raise RuntimeError(f"untouched line {index} changed")
        if any(line.error for line in after.lines):
            raise RuntimeError("a line is no longer valid JSON")
        remaining_lost, remaining_tail = split_unanswered_calls(after.lines)
        remaining = [item for item in remaining_lost + remaining_tail if not item[3]]
        if remaining:
            raise RuntimeError(
                f"{len(remaining)} unanswered client call(s) remain after the write"
            )
    except Exception as exc:  # noqa: BLE001 - any failure must roll back
        _atomic_restore(backup / "session.jsonl", session.path)
        print(f"error: {exc}; original restored from {backup}", file=sys.stderr)
        return 2

    after_hash = sha256_file(session.path)
    print(f"backup: {backup}")
    print(f"sha256: {before_hash[:16]}… -> {after_hash[:16]}…")
    print(
        f"verified: {len(plan)} call(s) answered, "
        f"{len(touched)} line(s) rewritten, {len(appended)} record(s) appended"
    )
    return 0


def cmd_restore(args) -> int:
    backup_dir = Path(args.backup).expanduser()
    manifest_path = backup_dir / "manifest.json"
    source = backup_dir / "session.jsonl"
    if not source.is_file() or not manifest_path.is_file():
        print(f"error: {backup_dir} is not a thread-doctor backup", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = Path(args.target or manifest["source_path"]).expanduser()
    expected = manifest.get("sha256")
    actual = sha256_file(source)
    if expected and actual != expected:
        print("error: backup checksum does not match its manifest", file=sys.stderr)
        return 1
    _atomic_restore(source, target)
    print(f"restored {target} from {source}")
    print(f"sha256: {actual}")
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-home", help="override ~/.claude (or set CTD_CLAUDE_HOME)")
    parser.add_argument("--force", action="store_true", help="allow writing to a live session")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    tools = subparsers.add_parser("tools", help="remove tool definitions from prompt snapshots")
    tools.add_argument("--session", required=True)
    tools.add_argument("--drop", required=True, help="comma-separated tool names")
    tools.add_argument("--expect-count", type=int, help="exact number of removals required")
    tools.add_argument("--apply", action="store_true")
    _common(tools)
    tools.set_defaults(func=cmd_tools)

    tail = subparsers.add_parser("repair-tail", help="drop trailing non-JSON lines")
    tail.add_argument("--session", required=True)
    tail.add_argument("--expect-count", type=int)
    tail.add_argument("--apply", action="store_true")
    _common(tail)
    tail.set_defaults(func=cmd_repair_tail)

    calls = subparsers.add_parser(
        "unanswered-calls", help="fill tool calls that never received a result"
    )
    calls.add_argument("--session", required=True)
    calls.add_argument("--expect-count", type=int)
    calls.add_argument("--result-text", help="content for the synthetic tool_result")
    calls.add_argument("--apply", action="store_true")
    _common(calls)
    calls.set_defaults(func=cmd_unanswered_calls)

    restore = subparsers.add_parser("restore", help="restore a backup")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--target")
    restore.add_argument("--claude-home")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    home = claude_home(getattr(args, "claude_home", None))
    try:
        if args.command == "restore":
            return cmd_restore(args)
        return args.func(args, home)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
