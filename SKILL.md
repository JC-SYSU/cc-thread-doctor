---
name: cc-thread-doctor
description: Diagnose and repair Claude Code session JSONL files that no longer resume or that fail every request with a provider schema error. Use when a Claude Code session cannot resume, when an old session returns HTTP 400 on every message (for example "Invalid schema for function 'Artifact' ... is not a regex"), when /resume is slow or fails to parse, or when a transcript is suspected of being truncated or oversized.
---

# Claude Code Thread Doctor

Repairs files, not Claude Code itself. A session is a plaintext JSONL file under
`~/.claude/projects/<project-dir>/<session-id>.jsonl`.

## Workflow

1. **Audit first, always read-only.**

   ```bash
   python3 scripts/doctor_audit.py --session <id>
   python3 scripts/doctor_audit.py --all
   ```

2. **Explain the finding before changing anything.** Each finding maps to a
   section in `references/failure-modes.md`. Distinguish verified modes from
   detected-only ones.

3. **Dry run the treatment.** `doctor_fix.py` writes nothing without `--apply`.

4. **Ask before applying.** A fix rewrites a file the user may still need. The
   command is reversible through the backup, but say what will change and which
   line numbers are affected.

5. **Verify.** Static verification is free. Shadow resume spends tokens and
   needs explicit approval:

   ```bash
   python3 scripts/doctor_verify.py --session <id>
   python3 scripts/doctor_verify.py --session <id> --shadow-resume --allow-cost
   ```

## Treatments in v0.1

```bash
# Remove a tool definition recorded in prompt snapshots
python3 scripts/doctor_fix.py tools --session <id> --drop Artifact --apply

# Drop truncated trailing lines
python3 scripts/doctor_fix.py repair-tail --session <id> --apply

# Restore a backup
python3 scripts/doctor_fix.py restore --backup <dir>
```

## Rules

- Never edit a transcript by hand when a treatment exists. Hand edits skip the
  backup, the byte-preservation check, and the verification.
- Never re-serialize the whole file. Only the treatment's target records may
  change; every other line must keep its original bytes.
- A session that is open in a running `claude` process must be closed before a
  fix. `--force` exists for the case where the user confirms.
- Text that mentions an old error is evidence, not a defect. Do not "clean" it.
- Shadow resume runs the user's hooks and contacts their provider. Treat it as
  a spend and say so first.

## Reading the result

- `artifact-in-snapshot`: the session replays a tool that current settings
  disable. See failure mode 1.
- `re2-incompatible-schema`: any tool whose `pattern` uses lookaround,
  backreferences, atomic groups, conditionals, `\G` or `\Z`. See mode 2.
- `truncated-tail`: the last write was cut off. See mode 3.
- `dangling-tool-use`: a call lost its result mid-transcript. Detected only.
- `unanswered-final-turn`: normal while a turn runs; a defect if interrupted.
- `oversized-session`: check the byte attribution before proposing a slim.
