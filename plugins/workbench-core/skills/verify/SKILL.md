---
name: verify
description: Record evidence that a change passed its checks (commands, exit codes, output hashes, tied to the exact content) and gate merges on it plus a "ship" review by the other agent. Use when opening or merging a PR, when the user says "verify", "evidence", "merge gate", "驗證", or when the CI verify gate fails.
license: MIT
---

# Verify

Evidence is tied to a content id: the git tree of the repo without `.ai/`. It is the same before and after you commit the same files, and any later code change invalidates it. `<skill-dir>` is the directory containing this file. Run commands from the repo root.

## Steps
1. Finish the change. The checks to run are listed in `.ai/verify.json`.
2. Get an independent review of the final content with the `cross-review` skill, which saves a JSON record. If the verdict is not `ship`, fix the findings and review again; `--base <last reviewed commit>` keeps the re-review small.
3. Record the evidence: `python <skill-dir>/scripts/verify.py run --host <claude|codex|human>`. It runs the checks and attaches matching reviews. If you reviewed after running, use `verify.py collect` instead of running again.
4. Commit `.ai/verifications/` with the change.
5. Confirm: `python <skill-dir>/scripts/verify.py gate`. CI runs the same gate on pull requests.

## Rules
- Never edit evidence files by hand, and never claim a check passed without a run that shows it.
- If the gate reports that content changed, run step 2 and 3 again on the new content.
- The author of a change cannot supply its `ship` review: the gate accepts only reviews where the peer differs from the author.
- Evidence records what was checked; it is not a security control.
