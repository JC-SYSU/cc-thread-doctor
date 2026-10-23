#!/usr/bin/env python3
"""Claude Code Thread Doctor — shared library.

Zero third-party dependencies, Python 3.11+.

Claude Code keeps each session as plaintext JSONL under
``~/.claude/projects/<sanitized-project-dir>/<session-id>.jsonl``. The format is
internal and changes between releases, so this library follows one rule when
editing: every line the tool does not touch is written back byte-for-byte.

Every write path follows the same discipline:
  1. backup_session()   -> byte-identical copy + SHA-256 + manifest, read back
  2. edit records       -> only touched lines are re-serialized
  3. atomic_write()     -> same-directory temp file + fsync + os.replace
  4. re-read and assert -> the promised invariant actually holds
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__version__ = "0.3.0"

DEFAULT_BACKUP_SUBDIR = ".cc-thread-doctor-backups"

# Record types observed in Claude Code 2.1.x transcripts. Unknown types are
# kept verbatim and only reported: the format is not public and grows over time.
KNOWN_RECORD_TYPES = {
    "user", "assistant", "attachment", "system", "mode", "permission-mode",
    "ai-title", "last-prompt", "file-history-snapshot", "file-history-delta",
    "api-request", "api-request-shape", "api-request-blob", "cost-state",
    "atis-latch", "continued-in", "summary", "queue-operation",
}

# Regex features Go's regexp (RE2) cannot compile. Several gateway providers
# validate tool schemas with RE2 and reject the whole request when a `pattern`
# contains one of these, even if the tool is never called.
RE2_UNSUPPORTED = (
    ("lookahead", re.compile(r"\(\?[=!]")),
    ("lookbehind", re.compile(r"\(\?<[=!]")),
    ("backreference", re.compile(r"\\[1-9]")),
    ("named-backreference", re.compile(r"\\k[<']")),
    ("atomic-group", re.compile(r"\(\?>")),
    ("conditional", re.compile(r"\(\?\(")),
    ("perl-anchor", re.compile(r"\\[GZ]")),
)

BLOAT_WARN_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- primitives

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def claude_home(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("CTD_CLAUDE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


# ---------------------------------------------------------------- session IO

@dataclass(frozen=True)
class SessionRef:
    session_id: str
    project_dir: str
    path: Path
    size: int
    mtime: float


@dataclass
class Line:
    index: int
    raw: bytes
    obj: dict | None
    error: str | None = None


@dataclass
class SessionFile:
    path: Path
    lines: list[Line]
    ends_with_newline: bool
    raw_size: int


def _parse_line(raw: bytes) -> tuple[dict | None, str | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not UTF-8 ({exc})"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} at char {exc.pos}"
    if not isinstance(obj, dict):
        return None, "top-level JSON value is not an object"
    return obj, None


def read_session(path: Path) -> SessionFile:
    data = path.read_bytes()
    if data == b"":
        return SessionFile(path=path, lines=[], ends_with_newline=False, raw_size=0)
    ends = data.endswith(b"\n")
    parts = data.split(b"\n")
    if ends:
        parts = parts[:-1]
    lines = []
    for index, raw in enumerate(parts):
        obj, error = _parse_line(raw)
        lines.append(Line(index=index, raw=raw, obj=obj, error=error))
    return SessionFile(path=path, lines=lines, ends_with_newline=ends, raw_size=len(data))


def list_sessions(home: Path) -> list[SessionRef]:
    sessions: list[SessionRef] = []
    root = home / "projects"
    if not root.is_dir():
        return sessions
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        for path in sorted(project.glob("*.jsonl")):
            if path.name.startswith("."):
                continue
            try:
                stat_result = path.stat()
            except OSError:
                continue
            sessions.append(
                SessionRef(
                    session_id=path.stem,
                    project_dir=project.name,
                    path=path,
                    size=stat_result.st_size,
                    mtime=stat_result.st_mtime,
                )
            )
    return sessions


def find_session(home: Path, ident: str) -> SessionRef:
    sessions = list_sessions(home)
    matches = [session for session in sessions if session.session_id == ident]
    if not matches:
        matches = [session for session in sessions if session.session_id.startswith(ident)]
    if not matches:
        raise LookupError(f"no session matching {ident!r} under {home / 'projects'}")
    if len(matches) > 1:
        listing = "\n".join(f"  {item.session_id}  {item.project_dir}" for item in matches)
        raise LookupError(f"{ident!r} is ambiguous:\n{listing}")
    return matches[0]


# ---------------------------------------------------------------- inspection

def iter_tool_lists(lines: list[Line]):
    """Yield (line index, snapshot attachment, tools) for prompt snapshots.

    Only ``attachment.prompt_snapshot`` carries the tool definitions that a
    resumed session replays. ``api-request-shape`` records look similar but are
    diagnostic logs; editing them changes nothing the next request sends.
    """
    for line in lines:
        if not line.obj:
            continue
        attachment = line.obj.get("attachment")
        if not isinstance(attachment, dict):
            continue
        if attachment.get("type") != "prompt_snapshot":
            continue
        tools = attachment.get("tools")
        if isinstance(tools, list):
            yield line.index, attachment, tools


def tool_name(tool: object) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
    return None


def iter_patterns(node: object, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{key}" if path else key
            if key == "pattern" and isinstance(value, str):
                yield child, value
            else:
                yield from iter_patterns(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_patterns(value, f"{path}[{index}]")


def lint_re2(pattern: str) -> list[str]:
    return [name for name, regex in RE2_UNSUPPORTED if regex.search(pattern)]


def _iter_content_blocks(lines: list[Line]):
    for line in lines:
        if not line.obj:
            continue
        message = line.obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                yield line.index, block


def tool_call_report(lines: list[Line]):
    """Return (dangling tool_use, orphan tool_result) entries.

    A tool_use without a matching tool_result is what an interrupted turn
    leaves behind. The Anthropic messages API rejects the next request until
    the pair is restored or the call is removed.
    """
    uses: list[tuple[int, str | None, str | None, bool]] = []
    results: set[str] = set()
    for index, block in _iter_content_blocks(lines):
        block_type = block.get("type")
        # server_tool_use covers Anthropic-hosted tools (web search, web fetch).
        # Their results arrive as tool_result blocks in an assistant message.
        if block_type in ("tool_use", "server_tool_use"):
            uses.append(
                (index, block.get("id"), block.get("name"), block_type == "server_tool_use")
            )
        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str):
                results.add(tool_use_id)
    dangling = [
        (index, tool_use_id, name, is_server)
        for index, tool_use_id, name, is_server in uses
        if tool_use_id and tool_use_id not in results
    ]
    known = {tool_use_id for _, tool_use_id, _, _ in uses if tool_use_id}
    orphans = [
        (index, block.get("tool_use_id"))
        for index, block in _iter_content_blocks(lines)
        if block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
        and block["tool_use_id"] not in known
    ]
    return dangling, orphans


def split_unanswered_calls(lines: list[Line]):
    """Split unanswered calls into (lost, tail).

    ``lost`` entries sit before the last user record, so their result can never
    arrive. ``tail`` entries sit in the final assistant message and may simply
    still be running.
    """
    dangling, _ = tool_call_report(lines)
    last_user_index = max(
        (
            line.index
            for line in lines
            if line.obj and line.obj.get("type") == "user"
        ),
        default=-1,
    )
    lost = [item for item in dangling if item[0] < last_user_index]
    tail = [item for item in dangling if item[0] > last_user_index]
    return lost, tail


def content_stats(lines: list[Line]) -> dict:
    images = 0
    image_base64_bytes = 0
    largest_line_bytes = 0
    largest_line = -1
    for line in lines:
        if len(line.raw) > largest_line_bytes:
            largest_line_bytes = len(line.raw)
            largest_line = line.index
        if not line.obj:
            continue
        message = line.obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            candidates = []
            if block.get("type") == "image":
                candidates.append(block)
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                candidates.extend(
                    item for item in block["content"]
                    if isinstance(item, dict) and item.get("type") == "image"
                )
            for image in candidates:
                images += 1
                source = image.get("source")
                if isinstance(source, dict) and source.get("type") == "base64":
                    data = source.get("data")
                    if isinstance(data, str):
                        image_base64_bytes += len(data)
    return {
        "images": images,
        "image_base64_bytes": image_base64_bytes,
        "largest_line_bytes": largest_line_bytes,
        "largest_line": largest_line,
    }


def record_type_stats(lines: list[Line]) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    blob_bytes = 0
    for line in lines:
        if not line.obj:
            continue
        record_type = line.obj.get("type")
        key = record_type if isinstance(record_type, str) else "?"
        counts[key] = counts.get(key, 0) + 1
        if key == "api-request-blob":
            blob_bytes += len(line.raw)
    return counts, blob_bytes


def session_cwd(lines: list[Line]) -> str | None:
    """The last working directory recorded in the transcript, if any."""
    cwd: str | None = None
    for line in lines:
        if not line.obj:
            continue
        value = line.obj.get("cwd")
        if isinstance(value, str) and value:
            cwd = value
    return cwd


def session_title(lines: list[Line]) -> str:
    title = ""
    for line in lines:
        if line.obj and line.obj.get("type") == "ai-title":
            value = line.obj.get("aiTitle")
            if isinstance(value, str) and value.strip():
                title = value.strip()
    return title


# ---------------------------------------------------------------- settings

def load_settings(home: Path) -> dict:
    merged: dict = {}
    for name in ("settings.json", "settings.local.json"):
        path = home / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def artifact_disabled(home: Path) -> bool:
    if os.environ.get("CLAUDE_CODE_DISABLE_ARTIFACT") == "1":
        return True
    for name in ("settings.json", "settings.local.json"):
        path = home / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        env = data.get("env")
        if isinstance(env, dict) and str(env.get("CLAUDE_CODE_DISABLE_ARTIFACT")) == "1":
            return True
        if data.get("enableArtifact") is False:
            return True
        if data.get("disableArtifact") is True:
            return True
    return False


def cleanup_period_days(home: Path) -> int:
    """The effective cleanupPeriodDays, with the 30-day default applied."""
    value = load_settings(home).get("cleanupPeriodDays", 30)
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 30
    return days if days > 0 else 30


def anthropic_base_url(home: Path) -> str:
    env_value = os.environ.get("ANTHROPIC_BASE_URL")
    if env_value:
        return env_value
    settings = load_settings(home)
    env = settings.get("env")
    if isinstance(env, dict):
        return str(env.get("ANTHROPIC_BASE_URL") or "")
    return ""


def is_strict_regex_provider(home: Path) -> bool:
    base = anthropic_base_url(home).lower()
    return bool(base) and "api.anthropic.com" not in base


# Matches the claude executable as a command token, not the doctor's own
# "--claude-home" flag.
_CLAUDE_TOKEN = re.compile(r"(?:^|[\s/])claude(?:\s|$)", re.IGNORECASE)


def is_claude_process_line(line: str, session_id: str) -> bool:
    low = line.lower()
    return session_id.lower() in low and bool(_CLAUDE_TOKEN.search(low))


def session_is_live(session_id: str) -> bool:
    """True when a running ``claude`` process references this session id.

    The check requires both the id and a standalone ``claude`` token so the
    doctor's own command line (which may carry ``--claude-home``) does not
    count as a live session.
    """
    if os.name == "nt":
        return False
    try:
        output = subprocess.run(
            ["ps", "-axo", "command"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(is_claude_process_line(line, session_id) for line in output.splitlines())


# ---------------------------------------------------------------- write path

def backup_session(home: Path, session: SessionRef, reason: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = home / DEFAULT_BACKUP_SUBDIR / f"{session.session_id}-{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    copy_path = destination / "session.jsonl"
    before = sha256_file(session.path)
    shutil.copy2(session.path, copy_path)
    after = sha256_file(copy_path)
    if before != after:
        raise RuntimeError("backup copy does not match the source")
    manifest = {
        "tool": "cc-thread-doctor",
        "version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "session_id": session.session_id,
        "source_path": str(session.path),
        "backup_path": str(copy_path),
        "sha256": before,
        "size": session.path.stat().st_size,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return destination


def atomic_write_session(session_file: SessionFile, lines: list[Line]) -> None:
    data = b"\n".join(line.raw for line in lines)
    if session_file.ends_with_newline:
        data += b"\n"
    path = session_file.path
    descriptor, temp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".cc-thread-doctor-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def assert_untouched(before: SessionFile, after: SessionFile, touched: set[int]) -> None:
    if len(before.lines) != len(after.lines):
        raise RuntimeError(f"line count changed: {len(before.lines)} -> {len(after.lines)}")
    for old, new in zip(before.lines, after.lines):
        if old.index in touched:
            continue
        if old.raw != new.raw:
            raise RuntimeError(f"untouched line {old.index} changed")


def fresh_session_id() -> str:
    return str(uuid.uuid4())


def estimate_request_tokens(session_file: SessionFile) -> int:
    """Rough input-token estimate for a resumed request.

    Only user/assistant records are sent as messages; attachments, request
    logs and file snapshots are not. 3.5 bytes per token is a middle ground
    between English JSON and CJK text.
    """
    conversation_bytes = sum(
        len(line.raw)
        for line in session_file.lines
        if line.obj and line.obj.get("type") in ("user", "assistant")
    )
    return int(conversation_bytes / 3.5) + 10_000


# ---------------------------------------------------------------- audit

@dataclass
class Finding:
    code: str
    severity: str  # error | warning | info
    message: str
    line: int | None = None
    detail: str | None = None


@dataclass
class AuditReport:
    session: SessionRef
    title: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def worst_severity(self) -> str | None:
        order = {"error": 0, "warning": 1, "info": 2}
        if not self.findings:
            return None
        return min(self.findings, key=lambda item: order.get(item.severity, 3)).severity


def audit_session(home: Path, session: SessionRef) -> AuditReport:
    session_file = read_session(session.path)
    report = AuditReport(session=session, title=session_title(session_file.lines))

    invalid = [line for line in session_file.lines if line.error]
    if invalid:
        first_bad = invalid[0].index
        trailing = all(line.error for line in session_file.lines[first_bad:])
        if trailing:
            report.findings.append(
                Finding(
                    "truncated-tail",
                    "error",
                    f"{len(invalid)} trailing line(s) are not valid JSON; the last write was cut off",
                    line=first_bad,
                )
            )
        else:
            report.findings.append(
                Finding(
                    "corrupt-lines",
                    "error",
                    f"{len(invalid)} line(s) are not valid JSON",
                    line=first_bad,
                )
            )

    tool_lists = list(iter_tool_lists(session_file.lines))
    if artifact_disabled(home):
        hits = [
            line_index
            for line_index, _, tools in tool_lists
            if any(tool_name(tool) == "Artifact" for tool in tools)
        ]
        if hits:
            report.findings.append(
                Finding(
                    "artifact-in-snapshot",
                    "error",
                    "the recorded tool set contains Artifact while artifacts are disabled; "
                    "a resumed session replays it, and strict gateways reject its schema",
                    line=hits[-1],
                )
            )

    strict = is_strict_regex_provider(home)
    seen_regex_hits: set[tuple[int, str]] = set()
    for line_index, _, tools in tool_lists:
        for tool in tools:
            name = tool_name(tool) or "<unnamed>"
            schema = tool.get("input_schema") if isinstance(tool, dict) else None
            if not schema:
                continue
            for json_path, pattern in iter_patterns(schema):
                features = lint_re2(pattern)
                if not features:
                    continue
                key = (line_index, name)
                if key in seen_regex_hits:
                    continue
                seen_regex_hits.add(key)
                report.findings.append(
                    Finding(
                        "re2-incompatible-schema",
                        "error" if strict else "warning",
                        f"tool {name}: {json_path} uses {', '.join(features)}; "
                        "RE2-based gateways reject the whole request",
                        line=line_index,
                        detail=pattern,
                    )
                )

    _, orphans = tool_call_report(session_file.lines)
    # A tool_use that appears before the last user record has definitely lost
    # its result. One in the final assistant message may simply still be
    # running, so it is reported as a warning with that caveat instead of an
    # error.
    lost, tail = split_unanswered_calls(session_file.lines)
    lost_client = [item for item in lost if not item[3]]
    lost_server = [item for item in lost if item[3]]
    if lost_client:
        sample = ", ".join(
            f"{name or '?'}@{index}" for index, _, name, _ in lost_client[:4]
        )
        report.findings.append(
            Finding(
                "dangling-tool-use",
                "error",
                f"{len(lost_client)} tool_use block(s) have no tool_result ({sample})",
                line=lost_client[0][0],
            )
        )
    if lost_server:
        sample = ", ".join(
            f"{name or '?'}@{index}" for index, _, name, _ in lost_server[:4]
        )
        report.findings.append(
            Finding(
                "unanswered-server-tool",
                "warning",
                f"{len(lost_server)} server-hosted tool call(s) have no recorded result ({sample})",
                line=lost_server[0][0],
            )
        )
    if tail:
        report.findings.append(
            Finding(
                "unanswered-final-turn",
                "warning",
                f"the last turn ends with {len(tail)} unanswered tool call(s); "
                "normal while a turn is running, request-breaking if the session was interrupted",
                line=tail[0][0],
            )
        )
    if orphans:
        report.findings.append(
            Finding(
                "orphan-tool-result",
                "warning",
                f"{len(orphans)} tool_result block(s) reference a missing tool_use",
                line=orphans[0][0],
            )
        )

    stats = content_stats(session_file.lines)
    type_counts, blob_bytes = record_type_stats(session_file.lines)
    stats.update(
        {
            "lines": len(session_file.lines),
            "size": session_file.raw_size,
            "blob_bytes": blob_bytes,
            "record_types": type_counts,
        }
    )
    report.stats = stats

    if session_file.raw_size >= BLOAT_WARN_BYTES or stats["image_base64_bytes"] >= BLOAT_WARN_BYTES:
        report.findings.append(
            Finding(
                "oversized-session",
                "warning",
                f"session is {human_bytes(session_file.raw_size)}; "
                f"{stats['images']} inline image(s) hold {human_bytes(stats['image_base64_bytes'])}",
                line=stats["largest_line"] if stats["largest_line"] >= 0 else None,
            )
        )

    unknown = sorted(set(type_counts) - KNOWN_RECORD_TYPES)
    if unknown:
        report.findings.append(
            Finding(
                "unknown-record-types",
                "info",
                f"unrecognized record type(s): {', '.join(unknown)}",
            )
        )

    return report
