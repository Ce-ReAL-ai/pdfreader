# fetch_cache.ps1 - download the prebuilt OCR cache from GitHub Releases.
#
# Why this exists: the cache is 22 MB of OCR text, which is fine to ship but not
# something you want committed to the repo (and it is regenerated from PDFs you
# may not be allowed to redistribute). So it lives as a Release asset and this
# script pulls it into ./staging/ocr-cache, which make_release.py can consume:
#
#     powershell -ExecutionPolicy Bypass -File release\fetch_cache.ps1 `
#         -Repo yourname/pdfreader -Tag v1.0.0
#     python tools\make_release.py --cache-src staging\ocr-cache --archive 7z
#
# Exit codes: 0 ok, 1 download/verify failed, 2 bad parameters.
#
# NOTE: keep this file ASCII-only. PowerShell 5.1 reads .ps1 as ANSI unless there
# is a BOM, so Chinese characters here would be mangled.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Repo,          # owner/name
    [string]$Tag = "latest",
    [string]$Asset = "pdfreader-cache.zip",
    [string]$OutDir = "staging/ocr-cache",
    [string]$ExpectedSha256 = "",                          # optional: verify the download
    [switch]$Force
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoPath = $Repo.Trim()
if ($repoPath -notmatch '^[^/]+/[^/]+$') {
    Write-Host "[ERROR] -Repo must look like owner/name (got: '$Repo')"
    exit 2
}

$out = Join-Path (Get-Location) $OutDir
if ((Test-Path $out) -and (-not $Force)) {
    $existing = Get-ChildItem -Path $out -Directory | Where-Object { $_.Name -match '^[0-9a-f]{16}$' }
    if ($existing) {
        Write-Host "[SKIP] $out already holds $($existing.Count) cache dir(s). Use -Force to replace."
        exit 0
    }
}

$api = "https://api.github.com/repos/$repoPath/releases"
if ($Tag -eq "latest") { $api += "/latest" } else { $api += "/tags/$Tag" }

Write-Host "Querying $api"
try {
    $release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "pdfreader-fetch-cache" }
} catch {
    Write-Host "[ERROR] cannot read release info: $($_.Exception.Message)"
    exit 1
}

$assetInfo = $release.assets | Where-Object { $_.name -eq $Asset } | Select-Object -First 1
if (-not $assetInfo) {
    Write-Host "[ERROR] asset '$Asset' not found in release '$($release.tag_name)'."
    Write-Host "        available assets:"
    $release.assets | ForEach-Object { Write-Host "          - $($_.name)" }
    exit 1
}
Write-Host "Found asset '$Asset' ($([math]::Round($assetInfo.size / 1MB, 1)) MB) in $($release.tag_name)"

$tmp = Join-Path $env:TEMP ("pdfreader-cache-" + [guid]::NewGuid().ToString("N") + ".zip")
Write-Host "Downloading ..."
try {
    $browserHeaders = @{ "User-Agent" = "pdfreader-fetch-cache"; "Accept" = "application/octet-stream" }
    Invoke-WebRequest -Uri $assetInfo.browser_download_url -Headers $browserHeaders -OutFile $tmp -UseBasicParsing
} catch {
    Write-Host "[ERROR] download failed: $($_.Exception.Message)"
    exit 1
}

$actual = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToUpper()
Write-Host "SHA256: $actual"
if ($ExpectedSha256) {
    $want = $ExpectedSha256.Trim().ToUpper()
    if ($actual -ne $want) {
        Write-Host "[ERROR] SHA256 mismatch!"
        Write-Host "        expected $want"
        Write-Host "        actual   $actual"
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "[OK] SHA256 matches the expected value."
} else {
    Write-Host "[WARN] no -ExpectedSha256 given; integrity not checked."
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Expand-Archive -Path $tmp -DestinationPath (Split-Path $out -Parent) -Force
Remove-Item $tmp -Force -ErrorAction SilentlyContinue

# The archive stores everything under ocr-cache/; unwrap that if it landed as a child.
$inner = Join-Path (Split-Path $out -Parent) "ocr-cache"
if ((Test-Path $inner) -and ((Split-Path $out -Leaf) -ne "ocr-cache")) {
    Move-Item $inner $out -Force
}

$dirs = @(Get-ChildItem -Path $out -Directory | Where-Object { $_.Name -match '^[0-9a-f]{16}$' })
$size = 0
Get-ChildItem -Path $out -Recurse -File | ForEach-Object { $size += $_.Length }
Write-Host ("[OK] cache ready: {0} doc(s), {1:N1} MB -> {2}" -f $dirs.Count, ($size / 1MB), $out)
Write-Host "Next:  python tools\make_release.py --cache-src `"$OutDir`" --archive 7z"
exit 0
