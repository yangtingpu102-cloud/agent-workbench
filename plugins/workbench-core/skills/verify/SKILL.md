---
name: verify
description: Record evidence that a change passed its checks (commands, exit codes, output hashes, tied to the exact content) and gate merges on it plus a "ship" review by the other agent. Use when opening or merging a PR, when the user says "verify", "evidence", "merge gate", "驗證", or when the CI verify gate fails.
license: MIT
---

# Verify

Evidence is tied to a content id: the git tree of the repo without `.ai/`, except `.ai/verify.json`. Any later change to code or to the checks invalidates it. `<skill-dir>` is the directory containing this file. Run commands from the repo root.

## Steps
1. Finish and commit the change. The checks are listed in `.ai/verify.json`.
2. Get an independent review of the whole change with the `cross-review` skill: `--base <PR base>`, e.g. `--base main`. It saves a JSON record. If the verdict is not `ship`, fix the findings, commit, and review the whole change again. The gate only accepts a review that covers everything since the PR base.
3. Record the evidence on the clean tree: `python <skill-dir>/scripts/verify.py run --host <claude|codex|human> --base main`. It runs your checks plus any the base still requires (so changing a check proves both the old and the new command), and attaches matching reviews. If you reviewed after running, use `verify.py collect` instead.
4. Commit `.ai/verifications/`. Changing only files under `.ai/` keeps the evidence valid.
5. Confirm: `python <skill-dir>/scripts/verify.py gate --base main`. CI runs the same gate on pull requests.

## Rules
- Never edit evidence files by hand, and never claim a check passed without a run that shows it.
- The latest run and the latest independent review decide: an older pass does not outweigh a newer failure or `fix-first`.
- If the gate finds no evidence for the content, the code or checks changed after the run: repeat steps 2 and 3.
- The author of a change cannot supply its `ship` review: the gate accepts only reviews where the peer differs from the author.
- Evidence records what was checked; it is not a security control.
