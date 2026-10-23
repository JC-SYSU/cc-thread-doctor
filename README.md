# Claude Code Thread Doctor

[中文版](README.zh-CN.md)

Diagnoses and repairs Claude Code sessions that can no longer resume, that
fail every request with a provider-side schema error, or that have grown too
large to resume comfortably.

Every repair is explicit, backed up, written atomically, and re-read from disk
to prove the promised invariant still holds. A repair can then be proven
end-to-end by resuming a shadow copy with the real `claude` CLI.

Status: v0.3.0, early. The audit, the four included treatments, and the
snapshot utility are covered by tests. The remaining failure modes are documented with their evidence and
planned commands, and are marked as such.

## The failure this started with

Resuming an old session on a DeepSeek or Zhipu endpoint returned, on every
message:

```text
API Error: 400 Invalid schema for function 'Artifact':
"^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}\"\\\\./[\]]{1,200}$" is not a "regex"
```

The session was months old and had been recorded while the Artifact tool was
still enabled. `CLAUDE_CODE_DISABLE_ARTIFACT=1` was set in `settings.json`, and
new sessions were clean — but `--resume` replays the tool definitions stored in
the session's `prompt_snapshot`. Artifact's schema contains a negative
lookahead, which the provider's Go/RE2 validator cannot compile, so the whole
request was rejected.

The fix removes Artifact from the recorded snapshot. On a 229-session corpus
the audit found six more sessions carrying the same tool definition — each
would hit the same 400 the next time it was resumed.

## How it works

```text
audit (read-only) ──► backup + SHA-256 ──► fix (atomic, byte-preserving) ──► verify (static + shadow resume)
```

- **audit** never writes. It scans one session or the whole corpus and reports
  only machine-state problems.
- **preserve** copies the file, verifies the copy's checksum, and writes a
  manifest before anything changes.
- **treat** changes only the records a treatment names. Every other line is
  written back at its original bytes, so unknown and future record types
  survive untouched.
- **verify** re-reads the file; with `--shadow-resume` it copies the session to
  a fresh id, resumes the copy with the real CLI, and deletes the copy.

## What it fixes

Verified by tests and by the case above:

| Failure | Command |
| --- | --- |
| Tool definitions recorded in a snapshot that must not be sent (Artifact while artifacts are disabled) | `doctor_fix.py tools --drop Artifact --apply` |
| RE2-incompatible `pattern` in any recorded tool schema | `doctor_fix.py tools --drop <tool> --apply` |
| Trailing lines cut off by a crash (invalid JSON at the end) | `doctor_fix.py repair-tail --apply` |
| `tool_use` without a matching `tool_result` (interrupted turn) | `doctor_fix.py unanswered-calls --apply` |
| Oversized sessions (inline images, giant tool results) | `doctor_fix.py slim --images --tool-outputs 4000 --apply` |

Detected and reported; treatments planned:

| Failure | Status |
| --- | --- |
| Orphan `tool_result`, unknown record types | audit reports them |

Session files are also protected by `doctor_backup.py snapshot`, a
hardlink-based mirror of `~/.claude/projects` with retention and a verified
single-file restore.

## Quick start

```bash
# Fleet scan, read-only
python3 scripts/doctor_audit.py --all

# One session, machine-readable
python3 scripts/doctor_audit.py --session abc12345 --json

# Treat: dry run first, then apply
python3 scripts/doctor_fix.py tools --session abc12345 --drop Artifact
python3 scripts/doctor_fix.py tools --session abc12345 --drop Artifact --apply

# Give an interrupted tool call a synthetic error result
python3 scripts/doctor_fix.py unanswered-calls --session abc12345 --apply

# Replace inline images and truncate oversized tool results
python3 scripts/doctor_fix.py slim --session abc12345 --images
python3 scripts/doctor_fix.py slim --session abc12345 --tool-outputs 4000 --apply

# Verify statically, or with a real shadow resume (spends tokens)
python3 scripts/doctor_verify.py --session abc12345
python3 scripts/doctor_verify.py --session abc12345 --shadow-resume --allow-cost

# Snapshot the whole corpus and restore one session later
python3 scripts/doctor_backup.py snapshot --dry-run
python3 scripts/doctor_backup.py snapshot --keep 7
python3 scripts/doctor_backup.py restore --session abc12345
```

Exit codes: `0` clean or dry run, `1` findings that need attention, `2` the
tool itself failed. `doctor_audit.py --strict` also fails on warnings.

## Safety and data

- Fix backups live in `~/.claude/.cc-thread-doctor-backups/<session>-<timestamp>/`
  with a SHA-256 manifest. `doctor_fix.py restore` puts one back.
- `doctor_backup.py snapshot` mirrors `~/.claude/projects` into
  `~/.claude/.cc-thread-doctor-backups/projects-<timestamp>/`. Files unchanged
  since the previous snapshot are hardlinked; the first snapshot copies the
  whole corpus.
- Every write is a same-directory temp file plus `os.replace`; a failed
  assertion restores the backup and exits 2.
- The tool writes nothing until `--apply`, and refuses to touch a session whose
  id appears in a running `claude --resume` process unless `--force` is passed.
- Reports stay on the machine. No network calls. Shadow resume is the only
  operation that contacts a provider, and it runs the real CLI with your
  existing configuration.

## Limits

- Shadow resume proves the file is resumable on this machine with the current
  provider. It cannot promise a remote service accepts the transcript forever.
- The JSONL format is internal and changes between releases. v0.3 was developed
  against Claude Code 2.1.304; unknown record types are preserved and reported,
  but a future format change may need a new reader.
- `dangling-tool-use` in the final assistant message is reported as a warning:
  it is normal while a turn is running, and request-breaking only if the
  session was interrupted.
- The tool does not modify Claude Code, its hooks, or authentication.

## Tests

```bash
python3 -m pytest -q
```

The suite builds synthetic sessions for every failure mode; no real transcript
is committed.

## Layout

```text
scripts/doctor_lib.py       session reader, checks, backup and atomic write
scripts/doctor_audit.py     read-only scan
scripts/doctor_fix.py       treatments and restore
scripts/doctor_verify.py    static and shadow verification
scripts/doctor_backup.py    hardlink snapshots and single-file restore
references/session-jsonl.md what the file contains and what resume replays
references/failure-modes.md symptom, evidence, treatment, status
```

## License

MIT.
