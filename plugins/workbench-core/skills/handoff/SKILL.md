---
name: handoff
description: Write or resume a compact handoff in .ai/HANDOFF.md so the next session (Claude Code, Codex, or a human) continues without re-exploring the repo. Use when the user says "handoff", "交接", "save progress", "continue in Codex/Claude", "pick up where we left off", or at a natural stopping point of multi-session work.
license: MIT
---

# Handoff

Goal: the next agent gets oriented from one small file plus three git commands, instead of re-reading the repo.

## Write (ending or pausing)
1. Collect facts cheaply: `git status --short`, `git diff --stat`, `git log --oneline -5`. Do not re-read files you already know.
2. Overwrite `.ai/HANDOFF.md` with exactly this schema. Stay under 40 lines and write no prose paragraphs:

   ```
   # Handoff — <YYYY-MM-DD HH:MM UTC> — <agent id> on <branch> @ <short sha>
   ## Goal
   <one sentence: what "done" means>
   ## State
   <done | in progress | blocked>: <one line>
   ## Done
   - <change> (evidence: `<command>` → <result>)
   ## Next
   1. <most important next step, naming the file or function>
   ## Files
   - <path>: <why it matters>
   ## Verify
   `<one command that proves the work is correct>`
   ## Gotchas
   - <non-obvious trap, or failed approach and why it failed>
   ```
3. Record only facts verified in this session. Mark anything else `(unverified)`.
4. If `.ai/board.md` has your row, set it to `review` or `open` and clear the lease.

## Resume (starting)
1. Read `.ai/HANDOFF.md`, then `.ai/board.md` if it exists. Run `git status --short` and `git log --oneline -5`.
2. If commits after the handoff touch the listed Files, tell the user what drifted before acting.
3. Start from Next step 1. Do not explore outside Files unless a step fails.
4. Run the Verify command before claiming that earlier work still holds.
