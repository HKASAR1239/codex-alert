#!/bin/sh
# Optional Codex hook: stdin goes directly to the engine, never into shell code.
# No stdout, decision, or failure status may change Codex's approval behavior.
case "${1:-}" in
  PermissionRequest|PostToolUse|Stop|Interrupt) ;;
  *) exit 0 ;;
esac

engine="$HOME/Applications/Codex Alert.app/Contents/Resources/engine/codex-alert-engine"
if [ -x "$engine" ]; then
  "$engine" hook --event "$1" >/dev/null 2>&1
  exit 0
fi

# Source installs retain the small Python companion in Application Support.
package="$HOME/Library/Application Support/CodexAlert/app"
if [ -f "$package/codex_alert/attention.py" ] && command -v python3 >/dev/null 2>&1; then
  PYTHONPATH="$package" python3 -m codex_alert hook --event "$1" >/dev/null 2>&1
fi
exit 0
