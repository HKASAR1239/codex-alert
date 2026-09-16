#!/bin/bash
# One entry point: setup, status, tests, phone settings and uninstall.
set -u
project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Codex Alert currently supports macOS only." >&2
  exit 1
fi
python_bin=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    python_bin="$("$candidate" -c 'import sys; print(sys.executable)')"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  echo "Python 3.9+ is required. Install Apple's Command Line Tools with:" >&2
  echo "  xcode-select --install" >&2
  exit 1
fi
"$python_bin" "$project_dir/scripts/manage.py" "$@"
result=$?
if [[ -t 0 && -t 1 && "${CODEX_ALERT_NO_PAUSE:-0}" != "1" ]]; then
  printf '\nPress Enter to close… '
  read -r _
fi
exit "$result"
