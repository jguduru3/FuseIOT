#!/usr/bin/env bash
# Development setup for FuseIoT (Linux/macOS/Git Bash). Windows PowerShell: setup.ps1
set -euo pipefail

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

echo "Setting up FuseIoT with $($PYTHON --version)..."
if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
fi

if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
else
    # shellcheck disable=SC1091
    source .venv/Scripts/activate
fi

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

echo "Linting..."
ruff check fuseiot examples

echo "Running tests..."
pytest -q

echo
echo "Setup complete. Try:"
echo "  fuseiot --help"
echo "  fuseiot -c examples/demo.yaml serve     # dashboard at http://127.0.0.1:8080"
echo "  python examples/basic_relay.py"
