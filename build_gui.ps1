# Build the GUI-only plugin executable.
# The result intentionally depends on src/, assets/, deps/ and config.toml in
# the original AppleMusicDecrypt project directory.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv\python.exe"
if (-not (Test-Path $py)) { throw "Missing .venv python" }

& $py -m pip install pyinstaller --quiet
& $py -m PyInstaller --noconfirm --clean "AppleMusicDecryptGUI.spec"

Write-Host "Build complete: dist\AppleMusicDecryptGUI.exe"
Write-Host "This executable is a GUI plugin and must be copied to the original project root."
