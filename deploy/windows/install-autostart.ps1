# Makes RecipeCollater start at logon and opens its port on the firewall. Run once.
# The firewall rule needs an elevated (admin) PowerShell; the startup shortcut does not.
#
#   deploy\windows\install-autostart.ps1                       # uses dist\RecipeCollater.exe, port 80
#   deploy\windows\install-autostart.ps1 -ExePath C:\RecipeCollater\RecipeCollater.exe -Port 8765
param(
    [string]$ExePath = (Join-Path $PSScriptRoot "..\..\dist\RecipeCollater.exe"),
    [int]$Port = 80
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "RecipeCollater.exe not found at '$ExePath'. Build it first (deploy\windows\build_exe.ps1) or pass -ExePath."
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path

# 1. Start at logon: a shortcut in the user's Startup folder.
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "RecipeCollater.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = Split-Path -Parent $exe
$shortcut.Description = "RecipeCollater family recipe app"
$shortcut.Save()
Write-Host "Startup shortcut created: $lnk"

# 2. Firewall: allow inbound TCP on the app's port so other devices can reach it.
$rule = Get-NetFirewallRule -DisplayName "RecipeCollater" -ErrorAction SilentlyContinue
if ($rule) {
    Write-Host "Firewall rule 'RecipeCollater' already exists."
} else {
    try {
        New-NetFirewallRule -DisplayName "RecipeCollater" -Direction Inbound `
            -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
        Write-Host "Firewall rule added for inbound TCP $Port."
    } catch {
        Write-Warning "Could not add the firewall rule - run this in an ADMIN PowerShell. ($_)"
    }
}

# 3. Defender exclusion for the exe's folder. A one-file PyInstaller exe is deep-scanned on every
#    launch, which can make the first start after a build take minutes; excluding its folder fixes
#    that. (Admin only; safe for your own app.)
try {
    Add-MpPreference -ExclusionPath (Split-Path -Parent $exe) -ErrorAction Stop
    Write-Host "Defender exclusion added for $(Split-Path -Parent $exe) (faster launches)."
} catch {
    Write-Warning "Could not add the Defender exclusion - run this in an ADMIN PowerShell. ($_)"
}

Write-Host ""
Write-Host "Done. RecipeCollater will start at your next sign-in; launch it now by running the exe."
