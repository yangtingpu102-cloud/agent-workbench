@AGENTS.md

## Claude Code only
- Load the plugin for one session without installing it: `claude --plugin-dir plugins/workbench-core`.
- Check a plugin: `claude plugin validate plugins/<plugin> --strict`. Check projected token cost: `claude plugin details <plugin>`.
- Peer calls through `cross-review` take 1 to 5 minutes. Give the Bash call a 600000 ms timeout.
