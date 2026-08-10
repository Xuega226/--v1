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

if (-not (Test-Path -LiteralPath $wpfExe)) {
    if (-not (Test-Path -LiteralPath $dotnet)) {
        throw "Missing local .NET runtime: $dotnet"
    }
    & $dotnet build $wpfProject -c Release
    if ($LASTEXITCODE -ne 0) {
        throw "WPF build failed with code $LASTEXITCODE"
    }
}

$env:DOTNET_ROOT = Split-Path -Parent $dotnet
$env:PATH = "$env:DOTNET_ROOT;$env:PATH"

# Start-Process receives the executable and script as separate values. This
# avoids cmd.exe START's ambiguous quote parsing around executable paths.
Start-Process -FilePath $pythonw -ArgumentList @($coreScript) -WorkingDirectory $projectRoot -WindowStyle Hidden
Start-Sleep -Milliseconds 350

$existingWindow = Get-Process -Name 'Unnameko.Desktop' -ErrorAction SilentlyContinue
if (-not $existingWindow) {
    Start-Process -FilePath $wpfExe -WorkingDirectory $projectRoot
}
