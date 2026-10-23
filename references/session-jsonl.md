# Claude Code session JSONL

What the doctor reads, what resume replays, and which edits are safe.

Everything below was observed on transcript files written by Claude Code
2.1.304 (October 2026). The format is internal and changes between releases;
the tool preserves unknown record types byte-for-byte for that reason.

## Location

```text
~/.claude/projects/<project-dir>/<session-id>.jsonl
```

`<project-dir>` is the session's working directory with `/` replaced by `-`
(`/Users/example` becomes `-Users-example`). Each line is one JSON object, normally
terminated by `\n`; a crash can leave the final line truncated mid-JSON.

Related state, not part of the transcript:

```text
~/.claude/history.jsonl              prompt history
~/.claude/file-history/<session-id>/ file snapshots for rewind
~/.claude/session-env/<session-id>/  per-session environment state
```

## Record types

| type | role |
| --- | --- |
| `user`, `assistant` | conversation messages; `message.content` holds text, `tool_use`, `tool_result`, `thinking`, `image` blocks |
| `attachment` | injected context; the important one is `prompt_snapshot` |
| `system` | lifecycle and hook records |
| `mode`, `permission-mode` | session mode state |
| `ai-title`, `last-prompt` | UI metadata; the last record of each type wins |
| `file-history-snapshot`, `file-history-delta` | rewind state |
| `api-request`, `api-request-shape`, `api-request-blob` | diagnostic logs of requests already sent |
| `cost-state` | accumulated usage |
| `atis-latch`, `continued-in`, `summary` | session bookkeeping |

Unknown types are kept verbatim and reported as `unknown-record-types`.

## What resume replays

`attachment.prompt_snapshot` stores the prompt state from the moment the session
recorded it:

```json
{
  "type": "attachment",
  "attachment": {
    "type": "prompt_snapshot",
    "systemPrompt": ["..."],
    "tools": [{"name": "Artifact", "input_schema": {"...": "..."}}],
    "cliPrefix": "...",
    "reminderFold": "...",
    "echoWireToolInputs": false,
    "contextRendering": "..."
  }
}
```

On `--resume`, the client restores the recorded tool set instead of rebuilding
it from the current settings.

Verified on 2026-10-23:

- A session last written on 2026-09-09 recorded 14 tools, including `Artifact`
  and `SendFeedback`.
- Resumed the same day with `CLAUDE_CODE_DISABLE_ARTIFACT=1` present in
  `settings.json`, the first request's tool list matched the recorded 14 exactly
  (plus currently enabled web tools and MCP tools).
- The request failed with the provider's schema error. New sessions on the same
  machine did not send `Artifact`.
- Across 229 sessions, only this request carried `Artifact`.

Therefore `prompt_snapshot.tools` is the authoritative place to remove a tool
that a resumed session must not send.

`api-request`, `api-request-shape` and `api-request-blob` are logs. Editing them
changes nothing that is sent — a repair that left the recorded shape untouched
still resumed successfully.

## Tool schemas and provider validation

`input_schema` follows JSON Schema. A `pattern` string is compiled by the
provider before the model runs. Go's `regexp` (RE2) — used by several gateway
providers — cannot compile:

- lookahead `(?=`, `(?!`
- lookbehind `(?<=`, `(?<!`
- backreferences `\1`, `\k<name>`
- atomic groups `(?>`
- conditionals `(?(` 
- Perl anchors `\G`, `\Z`

`\p{...}` classes and non-capturing groups `(?:...)` are fine.

When one pattern fails, the provider rejects the entire request, even if the
tool is never called. Observed error shape:

```text
API Error: 400 Invalid schema for function 'Artifact':
"^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}\"\\\\./[\]]{1,200}$" is not a "regex"
(request_id: ...)
```

In the Artifact schema that pattern is `properties.field.pattern`. The same
schema also uses `(?!\.\.?(?:\/|$))` for `collection` and `doc_id`; a provider
reports whichever pattern it reaches first.

Anthropic's own endpoint and permissive gateways accept the same definitions.
The audit therefore raises this as an error only when `ANTHROPIC_BASE_URL`
points somewhere other than `api.anthropic.com`, and as a warning otherwise.

## Message structure

A tool call and its result pair by id:

```text
assistant content: {"type": "tool_use", "id": "toolu_...", "name": "Bash"}
user content:      {"type": "tool_result", "tool_use_id": "toolu_...", "content": "..."}
```

Anthropic-hosted tools use a different pair, observed in transcripts:

```text
assistant content: {"type": "server_tool_use", "id": "call_...", "name": "web_search_prime"}
assistant content: {"type": "tool_result", "tool_use_id": "call_...", "content": "..."}
```

The doctor accepts both when pairing. Treating `server_tool_use` as unknown
produced false orphan reports during development; the test suite does not cover
that shape yet, but the parser does.

## Safe-edit rules

1. **Untouched lines keep their exact bytes.** The format is internal; a full
   re-serialization can drop fields a future release depends on.
2. **Tool removal edits only `prompt_snapshot.tools`.** Request logs are
   evidence, not input.
3. **Machine-state repairs do not rewrite conversation text.** A transcript
   that mentions an old error is evidence, not a defect.
4. **Backup, atomic replace, then re-read and assert.** Every write path in this
   repository follows that order.
5. **Unknown record types are preserved.** New record types are reported, never
   normalized.
