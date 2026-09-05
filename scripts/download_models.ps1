<#
.SYNOPSIS
    Download the ZipVoice-Distill int8 TTS model and the vocos vocoder
    for the local CPU fallback provider (sherpa-onnx).

.DESCRIPTION
    Downloads from GitHub Releases:
      - sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2 -> models/zipvoice/
      - vocos_24khz.onnx -> models/zipvoice/vocos_24khz.onnx

    Idempotent: files that already exist are skipped.
    Use -Mirror to download via hf-mirror.com when GitHub is unreachable.

    This script runs on the host (dev machine or deploy target). Models are
    never downloaded inside the docker image; the container mounts models/.

.EXAMPLE
    .\scripts\download_models.ps1
    .\scripts\download_models.ps1 -Mirror
#>
[CmdletBinding()]
param(
    [switch]$Mirror,
    [string]$ModelDir = ""
)

$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $ModelDir) { $ModelDir = Join-Path $RootDir "models" }
$ZipDir = Join-Path $ModelDir "zipvoice"

$ArchiveName = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2"
$ArchiveUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/$ArchiveName"
$VocoderName = "vocos_24khz.onnx"
$VocoderUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/$VocoderName"

# Mirror mode: hf-mirror.com does not proxy GitHub release tarballs, so we
# fall back to per-file downloads from the upstream HuggingFace repos
# (verified reachable). lexicon.txt and espeak-ng-data only exist inside the
# GitHub tarball; if they are missing after a mirror run we warn and tell the
# operator to re-run without -Mirror for the G2P data files.
$MirrorFiles = @(
    # hf-mirror url, destination filename under models/zipvoice/
    @("https://hf-mirror.com/k2-fsa/ZipVoice/resolve/main/zipvoice_distill/text_encoder_int8.onnx", "encoder.int8.onnx"),
    @("https://hf-mirror.com/k2-fsa/ZipVoice/resolve/main/zipvoice_distill/fm_decoder_int8.onnx", "decoder.int8.onnx"),
    @("https://hf-mirror.com/k2-fsa/ZipVoice/resolve/main/zipvoice_distill/tokens.txt", "tokens.txt"),
    @("https://hf-mirror.com/datasets/jiangzhuo9357/sherpa-onnx-tts-models/resolve/main/vocos_24khz.onnx", "vocos_24khz.onnx")
)

# Files the archive must contain after extraction (checked for idempotency).
$RequiredModelFiles = @(
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "tokens.txt",
    "lexicon.txt"
)

function Test-ModelReady {
    foreach ($f in $RequiredModelFiles) {
        if (-not (Test-Path (Join-Path $ZipDir $f))) { return $false }
    }
    # espeak-ng-data is a directory
    if (-not (Test-Path (Join-Path $ZipDir "espeak-ng-data"))) { return $false }
    return $true
}

function Save-WithRetry {
    param([string]$Url, [string]$OutFile, [int]$Retries = 3)
    for ($i = 1; $i -le $Retries; $i++) {
        try {
            Write-Host "Downloading $Url"
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
            return
        } catch {
            Write-Warning "Attempt $i/$Retries failed: $($_.Exception.Message)"
            if ($i -eq $Retries) { throw }
            Start-Sleep -Seconds ([Math]::Pow(2, $i))
        }
    }
}

New-Item -ItemType Directory -Force -Path $ZipDir | Out-Null

if ($Mirror) {
    foreach ($pair in $MirrorFiles) {
        $dest = Join-Path $ZipDir $pair[1]
        if (Test-Path $dest) {
            Write-Host "$($pair[1]) already present - skipping download."
        } else {
            Save-WithRetry -Url $pair[0] -OutFile $dest
        }
    }
    foreach ($f in @("lexicon.txt", "espeak-ng-data")) {
        if (-not (Test-Path (Join-Path $ZipDir $f))) {
            Write-Warning ("$f is not available on the hf-mirror fallback. " +
                "Chinese G2P quality may degrade; re-run without -Mirror to fetch it from the GitHub tarball.")
        }
    }
    if (-not (Test-ModelReady)) {
        Write-Host ("Mirror run fetched the core ONNX/token files but the model " +
            "set is incomplete. Re-run without -Mirror, or place lexicon.txt " +
            "and espeak-ng-data/ under $ZipDir manually.")
        exit 1
    }
    Write-Host "Done (mirror). Models are in $ZipDir"
    exit 0
}

if (Test-ModelReady) {
    Write-Host "ZipVoice model already present in $ZipDir - skipping download."
} else {
    $ArchivePath = Join-Path $ModelDir $ArchiveName
    Save-WithRetry -Url $ArchiveUrl -OutFile $ArchivePath

    Write-Host "Extracting $ArchiveName ..."
    # bsdtar (bundled with Windows 10+) handles .tar.bz2.
    $ExtractTmp = Join-Path $ModelDir ".zipvoice-extract"
    New-Item -ItemType Directory -Force -Path $ExtractTmp | Out-Null
    tar -xjf $ArchivePath -C $ExtractTmp
    if ($LASTEXITCODE -ne 0) { throw "tar extraction failed with exit code $LASTEXITCODE" }

    # Move the single top-level folder's contents into models/zipvoice/
    $top = Get-ChildItem -Path $ExtractTmp -Directory | Select-Object -First 1
    if (-not $top) { throw "Archive did not contain expected top-level directory" }
    Get-ChildItem -Path $top.FullName | Move-Item -Destination $ZipDir -Force

    Remove-Item -Recurse -Force $ExtractTmp
    Remove-Item -Force $ArchivePath
}

$VocoderPath = Join-Path $ZipDir $VocoderName
if (Test-Path $VocoderPath) {
    Write-Host "Vocoder $VocoderName already present - skipping download."
} else {
    Save-WithRetry -Url $VocoderUrl -OutFile $VocoderPath
}

if (-not (Test-ModelReady) -or -not (Test-Path $VocoderPath)) {
    throw "Model download incomplete. Re-run the script or try -Mirror."
}

Write-Host "Done. Models are in $ZipDir"

# ---------------------------------------------------------------------------
# Whisper ASR models (faster-whisper small + medium) for the ASR engine.
# Downloaded per-file from hf-mirror.com (GitHub does not host these).
# Idempotent: a model dir is skipped when model.bin already exists.
# ---------------------------------------------------------------------------

$WhisperDir = Join-Path $ModelDir "whisper"

function Test-WhisperReady {
    param([string]$Name)
    Test-Path (Join-Path $WhisperDir (Join-Path $Name "model.bin"))
}

function Save-WhisperModel {
    param([string]$Name)
    $dest = Join-Path $WhisperDir $Name
    if (Test-WhisperReady $Name) {
        Write-Host "Whisper $Name already present in $dest - skipping download."
        return
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $base = "https://hf-mirror.com/Systran/faster-whisper-$Name/resolve/main"
    foreach ($f in @("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")) {
        $out = Join-Path $dest $f
        if (Test-Path $out) {
            Write-Host "$f already present - skipping download."
        } else {
            Save-WithRetry -Url "$base/$f" -OutFile $out
        }
    }
    if (-not (Test-WhisperReady $Name)) {
        throw "Whisper $Name download incomplete. Re-run the script."
    }
}

Save-WhisperModel "small"
Save-WhisperModel "medium"

Write-Host "Done. Whisper models are in $WhisperDir"
