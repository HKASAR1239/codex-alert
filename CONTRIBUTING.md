# Contributing

Small, focused pull requests and reproducible bug reports are welcome.

1. Fork the repository and create a branch.
2. Make your change; keep the Python runtime dependency-free.
3. Run `python3 -m unittest discover -s tests -v` and `bash -n install.command`.
4. Describe the behavior changed and the checks you ran.

For macOS-specific changes, also check `bash install.command --check` and test the relevant behavior on a Mac. Unit tests must use synthetic sessions and mocked notification requests, never real conversations or real ntfy topics. Update the README if setup or behavior changes.

To build the self-contained menu bar app, follow [Building](docs/BUILDING.md). Runtime Python code stays standard-library-only; certifi and PyInstaller are isolated build dependencies. The build checks the frozen engine with private temporary settings and no external Python on `PATH`.

Bug reports should include the macOS version, Python version, reproduction steps and sanitized status output. Never attach configuration, session logs, phone numbers or notification topics. Report security concerns through GitHub's private vulnerability reporting when available; do not post working secrets publicly.
