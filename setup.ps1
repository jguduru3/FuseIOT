# Development setup for FuseIoT on Windows PowerShell.
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Write-Host "Linting..."
& .\.venv\Scripts\ruff.exe check fuseiot examples
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running tests..."
& .\.venv\Scripts\pytest.exe -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Setup complete. Activate with .\.venv\Scripts\Activate.ps1, then try:"
Write-Host "  fuseiot -c examples/demo.yaml serve     # dashboard at http://127.0.0.1:8080"
