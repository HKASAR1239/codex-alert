# Optional approval notifications

Codex Alert includes official lifecycle hooks in `hooks/hooks.json`. Install the repository as a Codex plugin using Codex's supported plugin setup, then review and trust its hooks in Codex. Installing the Mac app alone does not enable these hooks. See [Codex hooks](https://learn.chatgpt.com/docs/hooks) for the current review and trust flow.

The hooks never grant or reject requests. They return no decision and leave Codex's approval screen unchanged. They only write local notification metadata, then the normal Codex Alert watcher sends an ntfy alert.

- `PermissionRequest` records that Codex asked for approval.
- `PostToolUse` clears the matching request when the tool returns.
- `Stop` and `Interrupt` clear requests for that turn.
- Unsent requests expire after 10 minutes and are checked again just before delivery.

The message says **Codex requested approval**. Codex does not provide an approval-accepted lifecycle hook, so a long-running command may already have been approved when the alert arrives. These hooks do not detect every question or claim a task is currently blocked.

Only identifiers, timestamps and an opaque fingerprint are stored. Tool arguments are used in memory to match requests; commands, arguments, questions and decisions are not saved or sent. You can review the short shell wrapper and `src/codex_alert/attention.py` before trusting the hooks. The app indicates when hook metadata has been observed; this is not proof that every future request will be detected.
