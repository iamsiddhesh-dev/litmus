# Dev helpers for the Litmus platform (Windows local AVD).
#
#   .\scripts\dev.ps1 doctor      # preflight
#   .\scripts\dev.ps1 db          # apply schema
#   .\scripts\dev.ps1 api         # API on 127.0.0.1:8000
#   .\scripts\dev.ps1 worker      # Arq worker
#   .\scripts\dev.ps1 emulator    # start the local AVD (no snapshot)

param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'db', 'api', 'worker', 'emulator', 'mock-api')]
    [string]$Command = 'doctor'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv at $python - create one with Python 3.12 first." -ForegroundColor Red
    exit 1
}

switch ($Command) {
    'doctor' {
        & (Join-Path $RepoRoot '.venv\Scripts\mqa.exe') doctor
    }
    'db' {
        & $python 'scripts\bootstrap_db.py'
    }
    'api' {
        & $python -m litmus.api
    }
    'worker' {
        & (Join-Path $RepoRoot '.venv\Scripts\arq.exe') litmus.worker.WorkerSettings
    }
    'emulator' {
        $sdk = $env:ANDROID_HOME
        if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
        if (-not $sdk) { Write-Host 'ANDROID_HOME is not set.' -ForegroundColor Red; exit 1 }
        & (Join-Path $sdk 'emulator\emulator.exe') -avd pricebook_avd -no-snapshot -no-boot-anim -no-audio -gpu swiftshader_indirect
    }
    'mock-api' {
        # Read the mock API command from the fixture so no app-specific path is
        # hardcoded in the platform.
        $fixture = Join-Path $RepoRoot 'fixtures\bazaar.yaml'
        $cmd = (& $python -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1],encoding='utf-8'))['mock_api']['cmd'])" $fixture)
        & $python ($cmd -replace '^python\s+', '')
    }
}
