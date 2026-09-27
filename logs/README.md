# AI coding-session log

`session-transcript.jsonl` is the complete transcript of the Claude Code session that built this project, exported with the Claude desktop app's transcript export. It covers the whole session, from the original brief through every milestone (M1–M12), the viewer, DECISIONS.md and this final audit, up to the moment of export. The few final-audit steps after the export (committing this folder and the final report) are not in it.

Format: one JSON object per line (user messages, assistant messages, tool calls and tool results, in order). The 44 embedded images are screenshots taken during the session (mostly viewer and page-render checks).

Changes from the raw export:
- The user's email address (5 occurrences, all in app-injected context or a session summary) was replaced with `[email redacted]`. Nothing else was edited.
- The export's app-internal files (`metadata.json`, `local-session-state.json`, `custom-title.json`: UI state and connector configuration, no coding history) were not included.

Checked before committing: no API keys, access tokens or private keys. The only credentials that appear are the local test PostgreSQL credentials that are also in `docker-compose.yml` (`tickets`/`tickets`) and a throwaway calibration container password (`calib`).
