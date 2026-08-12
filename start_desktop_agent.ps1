$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$coreScript = Join-Path $projectRoot 'desktop_agent_core.py'
$dotnet = Join-Path $projectRoot '.dotnet\dotnet.exe'
$wpfProject = Join-Path $projectRoot 'desktop_agent\Unnameko.Desktop\Unnameko.Desktop.csproj'
$wpfExe = Join-Path $projectRoot 'desktop_agent\Unnameko.Desktop\bin\Release\net10.0-windows10.0.19041.0\Unnameko.Desktop.exe'

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Missing Python runtime: $pythonw"
}
if (-not (Test-Path -LiteralPath $coreScript)) {
    throw "Missing desktop core: $coreScript"
}
if (-not (Test-Path -LiteralPath $dotnet)) {
    throw "Missing local .NET runtime: $dotnet"
}

# The WPF executable is framework-dependent. Always expose the bundled runtime
# before building or starting it, even when the executable already exists.
$env:DOTNET_ROOT = Split-Path -Parent $dotnet
$env:PATH = "$env:DOTNET_ROOT;$env:PATH"

$wpfSourceRoot = Split-Path -Parent $wpfProject
$wpfNeedsBuild = -not (Test-Path -LiteralPath $wpfExe)
if (-not $wpfNeedsBuild) {
    $wpfBuiltAt = (Get-Item -LiteralPath $wpfExe).LastWriteTimeUtc
    $wpfNeedsBuild = [bool](Get-ChildItem -LiteralPath $wpfSourceRoot -File | Where-Object {
        $_.Extension -in @('.cs', '.xaml') -or $_.Name -eq 'Unnameko.Desktop.csproj'
    } | Where-Object { $_.LastWriteTimeUtc -gt $wpfBuiltAt } | Select-Object -First 1)
}
if ($wpfNeedsBuild) {
    & $dotnet build $wpfProject -c Release --no-restore
    if ($LASTEXITCODE -ne 0) {
        throw "WPF build failed with code $LASTEXITCODE"
    }
}

# One entry point can safely repair either half: do not duplicate an online
# core when only the WPF window needs to be reopened.
$existingCore = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and
    $_.CommandLine -match 'desktop_agent_core\.py' -and
    ($_.ExecutablePath -like "$projectRoot\*" -or $_.CommandLine -like "*$projectRoot*")
})
$coreUpdatedAt = (Get-Item -LiteralPath $coreScript).LastWriteTimeUtc
$staleCore = @($existingCore | Where-Object { $_.CreationDate.ToUniversalTime() -lt $coreUpdatedAt })
if ($staleCore) {
    # A Windows venv can expose both its launcher and the real interpreter.
    # Stopping one may make the other disappear, so address exact PIDs and
    # tolerate that normal race before checking the process list again.
    $staleCore | Sort-Object @{Expression={ if ($_.ExecutablePath -like "$projectRoot\*") { 1 } else { 0 } }} | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 300
    $existingCore = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -match 'desktop_agent_core\.py' -and
        ($_.ExecutablePath -like "$projectRoot\*" -or $_.CommandLine -like "*$projectRoot*")
    })
}
if (-not $existingCore) {
    Start-Process -FilePath $pythonw -ArgumentList @($coreScript) -WorkingDirectory $projectRoot -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
}

$existingWindow = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq 'Unnameko.Desktop.exe' -and $_.ExecutablePath -eq $wpfExe
}
if (-not $existingWindow) {
    Start-Process -FilePath $wpfExe -WorkingDirectory $projectRoot
}
