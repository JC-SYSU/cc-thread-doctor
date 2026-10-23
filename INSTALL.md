# Install

## Requirements

- Python 3.11 or newer
- Claude Code installed and on `PATH` (only `doctor_verify.py --shadow-resume` needs it)
- macOS or Linux; the audit, fixes and static verification also run on Windows

There is nothing to install: the scripts use only the standard library.

## Manual install

```bash
git clone https://github.com/JC-SYSU/cc-thread-doctor.git ~/cc-thread-doctor
cd ~/cc-thread-doctor

# Smoke test: help text, a scan, and a backup dry run
python3 scripts/doctor_audit.py --help
python3 scripts/doctor_audit.py --all
python3 scripts/doctor_backup.py snapshot --dry-run

# Optional: run the tests
python3 -m pytest -q
```

Point `--claude-home` (or `CTD_CLAUDE_HOME`) at a different Claude directory
when testing against a copy.

## Acceptance checklist

1. `doctor_audit.py --help` exits 0.
2. `doctor_audit.py --all` prints a table and exits 0 or 1.
3. `doctor_audit.py --session <one-real-id> --json` prints valid JSON.
4. `doctor_audit.py --session <one-real-id>` makes no change to the file
   (`shasum` before and after).
5. `doctor_fix.py tools --session <one-real-id> --drop Artifact` prints a plan
   or "nothing to do" and does not write.
6. `doctor_backup.py snapshot --dry-run` reports the file count and size and
   creates no directory.
7. `python3 -m pytest -q` passes.

Do not run `--apply` or `--shadow-resume` during installation.

## Agent install

Paste this into Codex, Claude Code, or another coding agent:

```text
You are installing cc-thread-doctor. The source is
https://github.com/JC-SYSU/cc-thread-doctor.

1. Clone it to ~/cc-thread-doctor and cd in.
2. Read INSTALL.md and execute the manual install steps.
3. Run the acceptance checklist and report each item's result.
4. Do not run doctor_fix.py --apply or doctor_verify.py --shadow-resume
   during installation. Those are user-directed operations.
```

## Using it as a Claude Code skill

Copy or symlink this repository into `~/.claude/skills/cc-thread-doctor/`.
`SKILL.md` describes when the agent should reach for it and which rules apply.
