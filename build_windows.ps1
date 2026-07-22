$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".python-build\python.exe"
$SystemIscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
$BundledIscc = Join-Path $Root ".inno-compiler\ISCC.exe"
$Iscc = if (Test-Path -LiteralPath $SystemIscc) { $SystemIscc } else { $BundledIscc }
$Installer = Join-Path $Root "dist-installer\Jarvis-Setup.exe"
$SelfTest = Join-Path $Root "dist-installer\packaged-self-test.json"
$Checksums = Join-Path $Root "dist-installer\SHA256SUMS.txt"

Set-Location $Root

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project Python runtime not found: $Python"
}
if (-not (Test-Path -LiteralPath $Iscc)) {
    throw "Inno Setup 6 compiler not found."
}

$Version = (Get-Content -Encoding UTF8 -LiteralPath "VERSION" -Raw).Trim()
$InstallerDefinition = Get-Content -Encoding UTF8 -LiteralPath "installer\Jarvis.iss" -Raw
if ($InstallerDefinition -notmatch ('#define MyAppVersion "' + [regex]::Escape($Version) + '"')) {
    throw "VERSION and installer/Jarvis.iss do not match."
}

& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Unit tests failed." }

& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Python dependency validation failed." }

& $Python tools\create_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

& $Python -m PyInstaller --noconfirm --clean Jarvis.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$SelfTestArguments = @(
    "--self-test",
    "--self-test-no-audio",
    ('"--self-test-output={0}"' -f $SelfTest)
)
$SelfTestProcess = Start-Process -FilePath (Join-Path $Root "dist\Jarvis\Jarvis.exe") -ArgumentList $SelfTestArguments -WindowStyle Hidden -Wait -PassThru
if ($SelfTestProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SelfTest)) {
    throw "Packaged self-test failed."
}

& $Iscc installer\Jarvis.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
if (-not (Test-Path -LiteralPath $Installer)) { throw "Installer was not created." }

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
Set-Content -Encoding ASCII -NoNewline -LiteralPath $Checksums -Value "$Hash  Jarvis-Setup.exe`n"
$Size = (Get-Item -LiteralPath $Installer).Length

Write-Host "Jarvis $Version built and verified."
Write-Host "Installer: $Installer"
Write-Host "Bytes: $Size"
Write-Host "SHA-256: $Hash"
