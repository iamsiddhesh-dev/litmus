# Build the demo APK with the B1 crash fixed.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# The benchmark fixture lives at E:\bazaar and is READ-ONLY: it is the thing
# being measured, so patching it would invalidate the benchmark. verify-fix
# needs a *different build* to prove a fix, so this script copies the app into a
# git-ignored scratch directory, applies the one-line B1 fix
# (`demo/CartStore.java.diff`), and builds the copy:
#
#   demo/.fixed-src/            scratch copy + Gradle output (git-ignored)
#   demo/fixed-bazaar.apk       committed build output (~30 KB)
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File demo\build_fixed_apk.ps1
#
# Requires: JDK 17 (JAVA_HOME or on PATH) and the fixture's Gradle wrapper.
# The first build resolves the Android Gradle Plugin from the network; later
# builds run from the Gradle cache.
[CmdletBinding()]
param(
    [string]$Fixture = "E:\bazaar",
    [string]$Out = "demo\fixed-bazaar.apk",
    [string]$JavaHome = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
#: Git-ignored scratch area: the repo never carries a second Android project.
$copy = Join-Path $repo "demo\.fixed-src"

if (-not (Test-Path $Fixture)) { throw "fixture not found: $Fixture (see fixtures/bazaar.yaml)" }

if ($JavaHome) { $env:JAVA_HOME = $JavaHome }
if (-not $env:JAVA_HOME) {
    $jdk = Get-ChildItem "C:\Program Files\Java" -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "bin\javac.exe") } |
        Select-Object -First 1
    if ($jdk) { $env:JAVA_HOME = $jdk.FullName }
}
if (-not $env:JAVA_HOME) { throw "JAVA_HOME is not set and no JDK was found under C:\Program Files\Java" }

Write-Host "fixture   $Fixture"
Write-Host "copy      $copy"
Write-Host "JAVA_HOME $env:JAVA_HOME"

# 1. Fresh copy of the app source. Build output and caches are excluded: they
#    are large, machine-specific, and would make the diff unreadable.
if (Test-Path $copy) { Remove-Item -Recurse -Force $copy }
New-Item -ItemType Directory -Path $copy | Out-Null
$items = @("app", "gradle", "gradlew", "gradlew.bat", "build.gradle", "settings.gradle",
           "gradle.properties", "local.properties")
foreach ($item in $items) {
    $source = Join-Path $Fixture $item
    if (Test-Path $source) { Copy-Item -Recurse -Force $source (Join-Path $copy $item) }
}
foreach ($junk in @("app\build", "build", ".gradle")) {
    $path = Join-Path $copy $junk
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}

# 2. The B1 fix: CartStore.add() incremented a null Integer on the second add
#    of the same product (NullPointerException). Every new entry now starts at
#    one, and the increment is null-safe. Same change as demo/CartStore.java.diff.
$cartStore = Join-Path $copy "app\src\main\java\com\bazaar\demo\CartStore.java"
$text = Get-Content $cartStore -Raw
$before = @'
        Entry e = new Entry();
        e.product = p;
        // qty is intentionally left null here; syncWithServer() was meant to set it.
        ITEMS.add(e);

        if (existing != null) {
            existing.qty = existing.qty + 1; // B1: NullPointerException on 2nd add
        }
'@
$after = @'
        Entry e = new Entry();
        e.product = p;
        e.qty = 1; // every new cart entry starts at one
        ITEMS.add(e);

        if (existing != null) {
            int previous = existing.qty == null ? 0 : existing.qty;
            existing.qty = previous + 1; // null-safe: fixed B1
        }
'@
if (-not $text.Contains($before)) { throw "B1 patch anchor not found in $cartStore (fixture changed?)" }
($text.Replace($before, $after)) | Set-Content -NoNewline $cartStore
Write-Host "patched   CartStore.java (B1)"

# 3. Build the copy with its own wrapper.
Push-Location $copy
try {
    & ".\gradlew.bat" assembleDebug --console=plain
    if ($LASTEXITCODE -ne 0) { throw "gradle assembleDebug failed with $LASTEXITCODE" }
} finally {
    Pop-Location
}

$built = Join-Path $copy "app\build\outputs\apk\debug\app-debug.apk"
if (-not (Test-Path $built)) { throw "build produced no APK at $built" }
$destination = Join-Path $repo $Out
Copy-Item -Force $built $destination
$size = (Get-Item $destination).Length
Write-Host ""
Write-Host "built     $destination  ($([math]::Round($size / 1KB, 1)) KB)"
Write-Host "verify    mqa verify-fix --finding F2 --run <run_id> --apk $Out"
