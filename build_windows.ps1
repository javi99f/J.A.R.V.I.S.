$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".python-build\python.exe"
$Iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"

Set-Location $Root
& $Python tools\create_icon.py
& $Python -m PyInstaller --noconfirm --clean Jarvis.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
& $Iscc installer\Jarvis.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
Write-Host "Installer created: dist-installer\Jarvis-Setup.exe"
