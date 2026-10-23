# Failure modes

Each entry records the symptom, the evidence in the session file, the cause,
the treatment, and how far the claim has been verified.

Status vocabulary:

- **Verified** — reproduced on a real session, with the fix confirmed.
- **Supported** — implemented in `doctor_fix.py` and covered by tests.
- **Detected** — `doctor_audit.py` reports it; no automatic treatment yet.
- **Hypothesis** — plausible and documented, not yet reproduced.

## 1. A disabled tool is replayed from the snapshot

**Status: Verified (2026-10-23), Supported.**

Symptom: every message in a resumed session returns HTTP 400, e.g.

```text
Invalid schema for function 'Artifact': "^(?!__.*__$)..." is not a "regex"
```

Evidence: `attachment.prompt_snapshot.tools` contains a tool that the current
settings no longer enable.

Cause: resume replays the recorded tool set. `CLAUDE_CODE_DISABLE_ARTIFACT=1`
and `enableArtifact: false` affect newly assembled sessions only.

Treatment:

```bash
python3 scripts/doctor_fix.py tools --session <id> --drop Artifact --apply
```

Then verify with `doctor_verify.py --shadow-resume`.

## 2. RE2-incompatible pattern in any tool schema

**Status: Verified mechanism (same family as #1), Supported.**

Symptom: a 400 naming the first tool whose `pattern` the provider cannot
compile. Zhipu reported it as `1210 API 调用参数有误` in September 2026;
DeepSeek reported `is not a "regex"` in October 2026.

Evidence: a `pattern` string under any tool's `input_schema` containing
lookahead, lookbehind, backreferences, atomic groups, conditionals, `\G` or
`\Z`.

Cause: the provider validates JSON Schema with Go's RE2, which does not support
those constructs. One bad pattern rejects the whole request.

Treatment: drop the offending tool, or switch to a provider that accepts the
pattern. The audit names the tool and the field; it checks every tool recorded
in every prompt snapshot, not just Artifact.

## 3. Truncated tail

**Status: Supported.**

Symptom: `claude --resume` fails to parse the session, or the session ends
mid-turn.

Evidence: the final line is not valid JSON, usually because the process died
while appending.

Treatment:

```bash
python3 scripts/doctor_fix.py repair-tail --session <id> --apply
```

The command refuses when invalid lines are followed by valid ones, because
dropping them would lose records that still parse.

## 4. Tool call with no result, mid-transcript

**Status: Detected.**

Symptom: the next request can be rejected for an invalid message sequence.

Evidence: a `tool_use` block with no matching `tool_result` and at least one
user record later in the file.

Treatment planned: either insert a synthetic `tool_result` with `is_error:
true`, or remove the call. Inserting keeps the model's reasoning attached to an
answer; removing is cleaner but changes the conversation. This needs its own
fixture and an `--expect-count` guard before it ships.

## 5. Unanswered call in the final turn

**Status: Detected, reported as a warning.**

Evidence: a `tool_use` after the last user record with no result.

This is normal while a turn is running — the transcript records the call before
the result arrives. It becomes a defect when the session was interrupted. The
audit cannot tell those apart without a reliable liveness signal (Claude Code
does not keep the file open between writes, so `lsof` does not help), so it
reports a warning and says why.

## 6. Server-hosted tool with no recorded result

**Status: Detected, warning.**

Evidence: a `server_tool_use` block (`web_search_prime`, `webReader`, …) with no
matching `tool_result` in a later assistant message.

Observed in older transcripts without breaking resume, so it is a warning. It
was the source of a false `orphan-tool-result` report until the parser learned
`server_tool_use`.

## 7. Orphan tool_result

**Status: Detected, warning.**

Evidence: a `tool_result` whose `tool_use_id` appears in no `tool_use` block.
Observed after history rewriting or compaction. Not yet linked to a specific
resume failure.

## 8. Oversized session

**Status: Detected.**

Evidence: file size, inline base64 images, giant `tool_result` text, and
`api-request-blob` records are summed and attributed.

Observed: sessions from 10 MB to 51 MB in a 229-session corpus. A 3.6 MB
session resumed with roughly 407k input tokens, which is roughly US$2 on a
low-cost provider and far more on a premium one.

Treatment planned (`slim`): replace inline images with a text placeholder,
truncate oversized tool results with a hash, optionally drop request blobs. Each
transform needs its own opt-in flag because it changes conversation content.

## 9. Thinking blocks signed by another provider

**Status: Hypothesis.**

When a session recorded on Anthropic's API is resumed against a third-party
endpoint, `thinking` blocks carry a `signature` the new provider may reject.
Not reproduced. Planned treatment: `strip-thinking`, with a shadow resume to
prove the request passes.

## 10. Stale tool descriptions or removed MCP tools

**Status: Hypothesis.**

Changelog entries state that resumed sessions replay recorded tool descriptions.
A tool that changed its API between releases, or an MCP server that is no longer
configured, can therefore be described incorrectly to the model. `tools
--reconcile` would compare the snapshot against the current configuration.

## 11. Session under the wrong project directory

**Status: Hypothesis.**

A session moved between machines, or a project directory that was renamed, can
disappear from `/resume` because the file sits under a `<project-dir>` that no
longer matches the working directory. `relocate` would move the file and report
the affected records.

## 12. Corrupted compaction summary

**Status: Hypothesis.**

If a `summary` record is truncated or references content that no longer exists,
resume after compaction may misbehave. No reproduction yet; the audit currently
only preserves such records.

## 13. Transcript loss from the cleanup period

**Status: Observed (2026-10-22). Prevention planned.**

Claude Code deletes session files older than `cleanupPeriodDays` (default 30)
on a rolling cleanup. A machine that had never raised the value lost 97
sessions; recovery required APFS and Time Machine snapshots.

Planned: `doctor_backup.py` (periodic snapshot of `projects/` with retention)
and an audit check that warns when `cleanupPeriodDays` is low relative to the
oldest session the user still references. This is data protection rather than
repair, but it addresses the most expensive failure observed so far.
