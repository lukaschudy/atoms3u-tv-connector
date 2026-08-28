# Contributing

Thanks for helping make the TV connector easier to build and safer to use.

## Before opening a change

1. Search existing issues and discussions.
2. For a new TV model or infrared code, include the exact model number and say
   whether the result was physically tested or only compiled/simulated.
3. Keep credentials, Wi-Fi names, local addresses, pairing files, recordings,
   and configured firmware images out of commits and screenshots.

## Development setup

```bash
# Windows PowerShell:
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -SkipPrepare
.\.venv\Scripts\python.exe -m unittest discover -s room_agent -v
# Linux/macOS:
bash scripts/setup.sh --skip-prepare
.venv/bin/python -m unittest discover -s room_agent -v
```

The setup wrappers create the repository-root virtual environment, enforce
Python 3.11 or newer, and install both the connector build tooling and host-test
dependencies.

Compile every firmware environment before submitting firmware changes:

```bash
# Windows PowerShell:
.\.venv\Scripts\python.exe -m platformio run -d room_agent\firmware\atoms3u_tv_connector
# Linux/macOS:
.venv/bin/python -m platformio run -d room_agent/firmware/atoms3u_tv_connector
```

## Pull requests

- Keep each pull request focused on one behavior.
- Add or update tests for host-side changes.
- Update the compatibility notes for hardware-facing changes.
- Distinguish `compiled`, `simulated`, and `physically validated` claims.
- Explain any security or compatibility trade-off.

By contributing, you agree that your contribution is licensed under the Apache
License 2.0 included in this repository.
