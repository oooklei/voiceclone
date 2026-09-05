#!/usr/bin/env bash
#
# Download the ZipVoice-Distill int8 TTS model and the vocos vocoder
# for the local CPU fallback provider (sherpa-onnx).
#
# Idempotent: files that already exist are skipped.
# Use --mirror to download via hf-mirror.com when GitHub is unreachable.
#
# This script runs on the host (dev machine or deploy target). Models are
# never downloaded inside the docker image; the container mounts models/.

set -euo pipefail

MIRROR=0
MODEL_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --mirror) MIRROR=1; shift ;;
        --model-dir) MODEL_DIR="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -z "${MODEL_DIR}" ]; then MODEL_DIR="${ROOT_DIR}/models"; fi
ZIP_DIR="${MODEL_DIR}/zipvoice"

ARCHIVE_NAME="sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2"
ARCHIVE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${ARCHIVE_NAME}"
VOCODER_NAME="vocos_24khz.onnx"
VOCODER_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/${VOCODER_NAME}"

# Mirror mode: hf-mirror.com does not proxy GitHub release tarballs, so we
# fall back to per-file downloads from the upstream HuggingFace repos
# (verified reachable). lexicon.txt and espeak-ng-data only exist inside the
# GitHub tarball; if they are missing after a mirror run we warn and tell the
# operator to re-run without --mirror for the G2P data files.
MIRROR_BASE="https://hf-mirror.com/k2-fsa/ZipVoice/resolve/main/zipvoice_distill"
MIRROR_VOCODER="https://hf-mirror.com/datasets/jiangzhuo9357/sherpa-onnx-tts-models/resolve/main/${VOCODER_NAME}"

model_ready() {
    for f in encoder.int8.onnx decoder.int8.onnx tokens.txt lexicon.txt; do
        [ -f "${ZIP_DIR}/${f}" ] || return 1
    done
    [ -d "${ZIP_DIR}/espeak-ng-data" ] || return 1
    return 0
}

download_with_retry() {
    url="$1"; out="$2"; retries=3
    for i in $(seq 1 "${retries}"); do
        echo "Downloading ${url}"
        if curl -fL --retry 2 -o "${out}" "${url}"; then
            return 0
        fi
        echo "attempt ${i}/${retries} failed" >&2
        sleep $((2 ** i))
    done
    return 1
}

mkdir -p "${ZIP_DIR}"

if [ "${MIRROR}" -eq 1 ]; then
    for pair in \
        "${MIRROR_BASE}/text_encoder_int8.onnx encoder.int8.onnx" \
        "${MIRROR_BASE}/fm_decoder_int8.onnx decoder.int8.onnx" \
        "${MIRROR_BASE}/tokens.txt tokens.txt" \
        "${MIRROR_VOCODER} ${VOCODER_NAME}"; do
        url="${pair% *}"; dest="${ZIP_DIR}/${pair#* }"
        if [ -f "${dest}" ]; then
            echo "$(basename "${dest}") already present - skipping download."
        else
            download_with_retry "${url}" "${dest}"
        fi
    done
    for f in lexicon.txt espeak-ng-data; do
        if [ ! -e "${ZIP_DIR}/${f}" ]; then
            echo "WARNING: ${f} is not available on the hf-mirror fallback." \
                "Chinese G2P quality may degrade; re-run without --mirror to fetch it from the GitHub tarball." >&2
        fi
    done
    if ! model_ready; then
        echo "Mirror run fetched the core ONNX/token files but the model set is incomplete." >&2
        echo "Re-run without --mirror, or place lexicon.txt and espeak-ng-data/ under ${ZIP_DIR} manually." >&2
        exit 1
    fi
    echo "Done (mirror). Models are in ${ZIP_DIR}"
    exit 0
fi

if model_ready; then
    echo "ZipVoice model already present in ${ZIP_DIR} - skipping download."
else
    ARCHIVE_PATH="${MODEL_DIR}/${ARCHIVE_NAME}"
    download_with_retry "${ARCHIVE_URL}" "${ARCHIVE_PATH}"

    echo "Extracting ${ARCHIVE_NAME} ..."
    EXTRACT_TMP="${MODEL_DIR}/.zipvoice-extract"
    mkdir -p "${EXTRACT_TMP}"
    tar -xjf "${ARCHIVE_PATH}" -C "${EXTRACT_TMP}"

    # Move the single top-level folder's contents into models/zipvoice/
    TOP="$(find "${EXTRACT_TMP}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    if [ -z "${TOP}" ]; then
        echo "archive did not contain expected top-level directory" >&2
        exit 1
    fi
    mv "${TOP}"/* "${ZIP_DIR}/"

    rm -rf "${EXTRACT_TMP}"
    rm -f "${ARCHIVE_PATH}"
fi

if [ -f "${ZIP_DIR}/${VOCODER_NAME}" ]; then
    echo "Vocoder ${VOCODER_NAME} already present - skipping download."
else
    download_with_retry "${VOCODER_URL}" "${ZIP_DIR}/${VOCODER_NAME}"
fi

if ! model_ready || [ ! -f "${ZIP_DIR}/${VOCODER_NAME}" ]; then
    echo "model download incomplete; re-run the script or try --mirror" >&2
    exit 1
fi

echo "Done. Models are in ${ZIP_DIR}"

# ---------------------------------------------------------------------------
# Whisper ASR models (faster-whisper small + medium) for the ASR engine.
# Downloaded per-file from hf-mirror.com (GitHub does not host these).
# Idempotent: a model dir is skipped when model.bin already exists.
# ---------------------------------------------------------------------------

WHISPER_DIR="${MODEL_DIR}/whisper"

whisper_ready() {
    [ -f "${WHISPER_DIR}/${1}/model.bin" ]
}

download_whisper_model() {
    name="$1"
    dest="${WHISPER_DIR}/${name}"
    if whisper_ready "${name}"; then
        echo "Whisper ${name} already present in ${dest} - skipping download."
        return 0
    fi
    mkdir -p "${dest}"
    base="https://hf-mirror.com/Systran/faster-whisper-${name}/resolve/main"
    for f in config.json model.bin tokenizer.json vocabulary.txt; do
        if [ -f "${dest}/${f}" ]; then
            echo "${f} already present - skipping download."
        else
            download_with_retry "${base}/${f}" "${dest}/${f}"
        fi
    done
    if ! whisper_ready "${name}"; then
        echo "Whisper ${name} download incomplete; re-run the script." >&2
        exit 1
    fi
}

download_whisper_model "small"
download_whisper_model "medium"

echo "Done. Whisper models are in ${WHISPER_DIR}"
