#!/bin/sh
# The CosyVoice3 weight set: what the loader actually opens, fetched once at
# BUILD time and asserted, with a boot-time fallback that should never fire.
#
# Why this is not just `snapshot_download(repo)` in the CMD, which is what it
# replaced. That call pulled the whole repository -- 9.75GB, measured against
# the HF API -- on every pod, on every boot, before uvicorn started. At twelve
# pods a book that is twelve downloads of the same bytes, and boot is 37% of
# the bill once generation is fast.
#
# Two separate savings, and they are worth keeping separate:
#
#   1. Only nine of the repository's nineteen files are ever opened. The other
#      4.33GB -- llm.rl.pt (2.02GB), speech_tokenizer_v3.batch.onnx (969MB),
#      flow.decoder.estimator.fp32.onnx (1.33GB), the README and an asset
#      image -- were downloaded and never read. That is a pure win whether the
#      weights are baked or not.
#   2. Baking the remaining 5.42GB into the image moves them from a per-boot
#      download to a per-host image layer, which RunPod caches across pods and
#      across books.
#
# The file list is NOT guesswork; every entry is a path the loader formats
# directly (cosyvoice/cli/cosyvoice.py, class CosyVoice3):
#
#   cosyvoice3.yaml              the config it refuses to start without
#   campplus.onnx                CosyVoiceFrontEnd, speaker embedding
#   speech_tokenizer_v3.onnx     CosyVoiceFrontEnd, speech tokenizer
#   llm.pt flow.pt hift.pt       CosyVoice3Model.load(), all three positional
#   CosyVoice-BlankEN/           qwen_pretrain_path override into the yaml
#
# spk2info.pt is guarded by os.path.exists in the frontend and is not in the
# repository at all. flow.decoder.estimator.fp32.onnx is read only when
# load_trt=True, which server/custom_api.py does not pass. Turning TensorRT on
# means adding it to BOTH lists below -- the patterns and the required set --
# so that a build which forgot it fails on a runner instead of a pod failing at
# load time.

# -f as well as -eu: the patterns are passed to the Hub, not to the shell, and
# an unquoted "CosyVoice-BlankEN/*" that happened to match something in the
# working directory would quietly fetch a different set of files.
set -euf

: "${COSYVOICE_MODEL_DIR:?COSYVOICE_MODEL_DIR is not set}"
: "${COSYVOICE_MODEL_REPO:?COSYVOICE_MODEL_REPO is not set}"
: "${COSYVOICE_MODEL_REVISION:?COSYVOICE_MODEL_REVISION is not set}"

ALL_PATTERNS="llm.pt flow.pt hift.pt cosyvoice3.yaml config.json configuration.json campplus.onnx speech_tokenizer_v3.onnx CosyVoice-BlankEN/*"

# What must exist afterwards. A subset of the patterns on purpose: it names the
# files whose absence stops the server, so the check reads as "can this image
# serve" rather than "did the download finish".
REQUIRED="cosyvoice3.yaml campplus.onnx speech_tokenizer_v3.onnx llm.pt flow.pt hift.pt CosyVoice-BlankEN/config.json CosyVoice-BlankEN/model.safetensors"

fetch() {
    # HF_HOME is redirected into /tmp for the length of this call rather than
    # left at its default. huggingface_hub below 0.23 fills local_dir with
    # symlinks into the blob cache for anything over 5MB, so the weights would
    # be stored twice in the image -- or, worse, pointed at /workspace, which a
    # RunPod network volume would mount over and leave dangling. Downloading
    # into a scratch HF_HOME, dereferencing, and deleting it leaves exactly one
    # real copy whatever version of the library resolved.
    #
    # PYTHON is overridable only so this script can be exercised outside the
    # image -- the interpreter inside it is always /opt/venv/bin/python.
    HF_HOME=/tmp/hf-build HF_HUB_ENABLE_HF_TRANSFER=1 \
    "${PYTHON:-/opt/venv/bin/python}" - "$@" <<'PY'
import os, sys
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ["COSYVOICE_MODEL_REPO"],
    revision=os.environ["COSYVOICE_MODEL_REVISION"],
    local_dir=os.environ["COSYVOICE_MODEL_DIR"],
    allow_patterns=list(sys.argv[1:]),
)
PY
    find "$COSYVOICE_MODEL_DIR" -type l -exec \
        sh -c 'cp --remove-destination "$(readlink -f "$1")" "$1"' _ {} \;
    rm -rf /tmp/hf-build "$COSYVOICE_MODEL_DIR/.cache"
}

check() (
    # A subshell, so the cd cannot follow the caller out of here.
    cd "$COSYVOICE_MODEL_DIR"
    for f in $REQUIRED; do
        [ -s "$f" ] || { echo ">>> missing weight: $COSYVOICE_MODEL_DIR/$f"; exit 1; }
    done
    # An LFS pointer is a couple of hundred bytes of text and downloads
    # successfully, so "the file is there" is not the same as "the weights are
    # there". Nothing in this set is legitimately under a megabyte.
    small="$(find . \( -name '*.pt' -o -name '*.onnx' -o -name '*.safetensors' \) -size -1M)"
    [ -z "$small" ] || { echo ">>> suspiciously small weight files:"; echo "$small"; exit 1; }
    dangling="$(find . -type l)"
    [ -z "$dangling" ] || { echo ">>> symlinks left in the model dir:"; echo "$dangling"; exit 1; }
    exit 0
)

case "${1:-ensure}" in
    fetch)
        shift
        [ "$#" -gt 0 ] || set -- $ALL_PATTERNS
        fetch "$@"
        ;;
    check)
        check || exit 1
        echo "weights present: $(du -sh "$COSYVOICE_MODEL_DIR" | cut -f1) in $COSYVOICE_MODEL_DIR"
        ;;
    ensure)
        # The boot-time fallback. It should never run on an image built by this
        # Dockerfile -- the build asserts the weights are in the layer -- but an
        # image built with the bake skipped still serves rather than crash-
        # looping, and the log line says which of the two happened.
        if check >/dev/null 2>&1; then
            echo "weights baked into the image; nothing to download"
        else
            echo ">>> weights not in the image; downloading them now (this costs GPU minutes)"
            fetch $ALL_PATTERNS
            check
        fi
        ;;
    *)
        echo "usage: $0 {fetch [pattern...]|check|ensure}" >&2
        exit 2
        ;;
esac
