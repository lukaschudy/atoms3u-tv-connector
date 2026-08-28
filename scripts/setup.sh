#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  printf '%s\n' "Python 3.11 or newer is required: https://python.org/downloads/" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python.exe ]; then
  "$PYTHON" -m venv .venv
fi
if [ -x .venv/bin/python ]; then
  VENV_PYTHON=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
  VENV_PYTHON=.venv/Scripts/python.exe
else
  printf '%s\n' "The virtual environment was created without a Python executable." >&2
  exit 1
fi

"$VENV_PYTHON" scripts/check_python.py
"$VENV_PYTHON" -m pip install --disable-pip-version-check \
  -r requirements-tv-connector.txt \
  -r room_agent/requirements-windows.txt
if ! "$VENV_PYTHON" -m pip check ||
   ! "$VENV_PYTHON" scripts/check_python.py --installed; then
  printf '%s\n' "The existing .venv is incompatible; remove it and run setup again." >&2
  exit 1
fi

printf '\n%s\n' "Tooling is ready."
if [ "${1:-}" != "--skip-prepare" ]; then
  "$VENV_PYTHON" room_agent/setup_tv_connector.py prepare
fi

printf '\n%s\n' "Next steps:"
printf '  %s %s\n' "$VENV_PYTHON" "room_agent/setup_tv_connector.py doctor"
printf '  %s %s\n' "$VENV_PYTHON" "room_agent/setup_tv_connector.py flash"
