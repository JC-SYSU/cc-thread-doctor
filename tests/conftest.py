from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


ARTIFACT_TOOL = {
    "name": "Artifact",
    "description": "publish output as a web page",
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "pattern": r"^(?!__.*__$)[^\p{Cc}]{1,200}$",
            }
        },
    },
}

EXA_TOOL = {
    "name": "mcp__exa__search",
    "description": "search",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "pattern": r"^[a-z]+(?: [a-z]+)*$"}
        },
    },
}

BASH_TOOL = {"name": "Bash", "description": "run a command", "input_schema": {"type": "object"}}


def snapshot_line(tools: list[dict], session_id: str, uuid: str = "snap-1") -> dict:
    return {
        "type": "attachment",
        "uuid": uuid,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "sessionId": session_id,
        "attachment": {
            "type": "prompt_snapshot",
            "systemPrompt": ["You are Claude Code."],
            "tools": tools,
        },
    }


def title_line(title: str, session_id: str) -> dict:
    return {"type": "ai-title", "aiTitle": title, "sessionId": session_id}


def user_line(text: str, session_id: str, uuid: str = "user-1") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "sessionId": session_id,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def tool_use_line(tool_id: str, name: str, session_id: str, uuid: str = "asst-1") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": {}}
            ],
        },
    }


def tool_result_line(tool_id: str, session_id: str, uuid: str = "result-1") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
            ],
        },
    }


@pytest.fixture
def make_session(tmp_path: Path):
    counter = {"n": 0}

    def _make(
        records: list[dict],
        session_id: str | None = None,
        project: str = "-Users-test",
        settings: dict | None = None,
        trailing_newline: bool = True,
    ):
        if session_id is None:
            counter["n"] += 1
            session_id = f"11111111-1111-1111-1111-{counter['n']:012d}"
        home = tmp_path / "home"
        project_dir = home / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        body = "\n".join(dump(record) for record in records)
        if trailing_newline:
            body += "\n"
        path.write_text(body, encoding="utf-8")
        if settings is not None:
            home.mkdir(parents=True, exist_ok=True)
            (home / "settings.json").write_text(
                json.dumps(settings, indent=2), encoding="utf-8"
            )
        return home, session_id, path

    return _make
