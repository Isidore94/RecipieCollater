# Builds recipecollater.exe from the repo root. Run from anywhere:
#   deploy\windows\build_exe.ps1
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$spec = Join-Path $repo "deploy\windows\RecipeCollater.spec"
$dist = Join-Path $repo "dist"
$work = Join-Path $repo "build"

Push-Location $repo
try {
    # --group build installs PyInstaller (kept out of the runtime dependency set).
    uv run --group build pyinstaller --noconfirm --clean --distpath $dist --workpath $work $spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

Copy-Item (Join-Path $repo "deploy\windows\.env.example") (Join-Path $dist ".env.example") -Force
Copy-Item (Join-Path $repo "deploy\windows\EXE_SETUP.md") (Join-Path $dist "EXE_SETUP.md") -Force

Write-Host ""
Write-Host "Built: $dist\RecipeCollater.exe"
Write-Host "Smoke-test it:  $dist\RecipeCollater.exe --smoke-test"
Write-Host "Then copy .env.example -> .env beside the exe and fill in your keys."
