[CmdletBinding()]
param(
    [string]$VideoAgentRoot = "D:\Hermes\video-agent",
    [string]$PatchPath = (Join-Path $PSScriptRoot "hermes-motioncraft-semantic-integration.patch")
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path $VideoAgentRoot).Path
$patch = (Resolve-Path $PatchPath).Path
$required = @(
    "scripts\podcast_clipper.py",
    "scripts\render_editorial_pro.py",
    "skills\podcast-video-clipper\SKILL.md",
    "motioncraft-renderer\package.json"
)

foreach ($relativePath in $required) {
    $candidate = Join-Path $root $relativePath
    if (-not (Test-Path $candidate -PathType Leaf)) {
        throw "File wajib tidak ditemukan: $candidate"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git tidak ditemukan di PATH. Instal/aktifkan Git for Windows lalu ulangi."
}

Push-Location $root
try {
    & git apply --check $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Patch tidak cocok dengan versi file saat ini. Tidak ada file yang diubah."
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupRoot = Join-Path $root "backups\motioncraft-integration-$stamp"
    foreach ($relativePath in $required[0..2]) {
        $source = Join-Path $root $relativePath
        $destination = Join-Path $backupRoot $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }

    & git apply --whitespace=nowarn $patch
    if ($LASTEXITCODE -ne 0) {
        throw "Patch gagal diterapkan. Backup tersedia di $backupRoot"
    }
}
finally {
    Pop-Location
}

$python = Join-Path $root ".venv\Scripts\python.exe"
$clipper = Join-Path $root "scripts\podcast_clipper.py"
$bridge = Join-Path $root "scripts\motioncraft_bridge.py"
$renderer = Join-Path $root "motioncraft-renderer"

Write-Host "Integrasi MotionCraft berhasil dipasang." -ForegroundColor Green
Write-Host "Backup: $backupRoot"
Write-Host ""
Write-Host "Jalankan verifikasi berikut:"
Write-Host ('& "{0}" "{1}" doctor --renderer "{2}"' -f $python, $bridge, $renderer)
Write-Host ('& "{0}" "{1}" doctor' -f $python, $clipper)
