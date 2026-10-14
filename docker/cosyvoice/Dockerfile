# CosyVoice3 serving image, with the Python environment already built.
#
# Why this exists. The first RunPod template ran server/runpod_start.sh at
# boot: it clones CosyVoice with submodules, apt-installs a toolchain, builds
# openai-whisper from an sdist, resolves a full requirements.txt and installs a
# CUDA 12.1 torch stack -- all on a billing GPU, every single boot, measured at
# 3.5-5 minutes when the network cooperates. That is the same shape of problem
# SAC-35 diagnosed for Voxtral, where a boot-time install was the difference
# between a pod that served in four minutes and one that never served at all.
#
# It also multiplies. Horizontal scaling means five pods each taking that risk
# independently, and one clone that flakes leaves a pod billing for nothing.
#
# ttsfrd needs cp310 (the only wheel published is
# ttsfrd-0.4.2-cp310-cp310-linux_x86_64.whl), and runpod_start.sh already skips
# it on any other Python and falls back to wetext. Baking the environment lets
# us simply pick 3.10 and keep the better text frontend rather than leaving it
# to whatever the host image happened to ship.
#
# The model weights ARE baked in, which reverses what this file used to say.
# The old reasoning ("a few GB, and it is one download") was wrong on both
# counts. The repository is 9.75GB, the CMD pulled all of it, and it pulled it
# on every boot of every pod -- twelve times a book once the pool is twelve
# pods, where boot is 37% of the bill. Only 5.42GB of it is ever opened; see
# docker/cosyvoice/weights.sh for the file-by-file argument.
#
# BAKE_WEIGHTS=0 turns this off, and the vLLM variant uses it: torch+vllm is
# ~10GB on its own and a GitHub runner has 50GB, which is not enough to hold
# the uncompressed layers and the compressed export at once. That variant
# fetches at boot instead, through the same `ensure` path.
#
# The honest cost, stated because it is a real trade and not a free win: the
# image grows by that 5.42GB, so a host that has never pulled it spends longer
# before the container starts. What makes it the right way round is that the
# image layer is cached per HOST and the download was paid per POD -- and
# PORT_MAPPING_TIMEOUT_SECONDS is already 600s for the cold-pull case. If a
# measured boot ever says otherwise, dropping the three fetch layers below
# restores the old behaviour exactly: weights.sh's `ensure` mode downloads at
# boot when the image has no weights in it.
# Two images are built from this one file, and the ARGs are the whole
# difference. Defaults reproduce :3.3 exactly -- the image that is live and
# has served -- so an unparameterised build cannot accidentally become the
# experimental one. `docker/cosyvoice/build-args.md` has both sets.
#
#   cu121 (default)  torch 2.3.1, no vLLM   -> Ampere/Ada/Hopper, sm_50..sm_90
#   cu128 + vLLM     torch 2.8.0, vllm      -> adds Blackwell sm_100/sm_120,
#                                              and batches the LLM
ARG CUDA_BASE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04
FROM ${CUDA_BASE}

# nvcc is needed at RUNTIME (see below) and its package name carries the CUDA
# minor version, so it travels with the base image rather than being hardcoded.
ARG NVCC_PKG=cuda-nvcc-12-1

# Empty means "no vLLM", which is the proven image. Set to a pin such as
# "vllm==0.11.0" to build the batching variant; upstream supports exactly
# 0.9.0 (legacy engine) and >=0.11.0 (V1 engine) and says 0.10.x is untested.
ARG VLLM_SPEC=
# Upstream pairs 4.51.3 with vllm 0.9.0 and 4.57.1 with 0.11.0. CosyVoice's own
# requirements.txt asks for 4.51.3, so the vLLM build has to re-pin AFTER it.
ARG TRANSFORMERS_SPEC=
ARG TORCH_SPEC="torch==2.3.1+cu121 torchvision==0.18.1+cu121 torchaudio==2.3.1+cu121"
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu121

# 22.04 rather than 24.04, and that is forced rather than conservative: it ships
# Python 3.10, which is the only version ttsfrd publishes a wheel for. On 24.04
# (3.12) the text frontend silently degrades to wetext.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HOME=/workspace/.cache/huggingface \
    COSYVOICE_MODEL_DIR=/opt/cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B \
    COSYVOICE_MODEL_REPO=FunAudioLLM/Fun-CosyVoice3-0.5B-2512 \
    VOICES_DIR=/workspace/voices \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/cosyvoice:/opt/cosyvoice/third_party/Matcha-TTS

# sox and ffmpeg are runtime dependencies, not build ones -- torchaudio shells
# out to them for resampling. build-essential and python3-dev stay in the final
# image for the same reason they do in the Voxtral one: anything that JIT-
# compiles on the pod needs a compiler and Python.h at SERVE time, and finding
# that out costs a GPU deploy rather than a free runner minute.
# cuda-nvcc is required AT RUNTIME and its absence is invisible on a CPU
# runner. modelscope's fix_transformers_upgrade imports transformers, which
# probes for a CUDA compiler -- but only when a GPU is actually present. So the
# build-time import check passes on a GitHub runner and the pod dies with
#     RuntimeError: Failed to import transformers.modeling_utils ...
#     [Errno 2] No such file or directory: '/usr/local/cuda/bin/nvcc'
# after it has pulled the whole image. The working gist template never hit this
# because runpod/pytorch:...-devel-... ships nvcc; this base is the -runtime-
# variant, which does not.
#
# The package rather than the devel base: cuda-nvcc-12-1 is about 60MB against
# roughly 3GB for nvidia/cuda:...-devel, and every cold pod pays that difference
# on every pull.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-venv python3.10-dev python3-pip \
        build-essential git curl ca-certificates \
        unzip ffmpeg psmisc sox libsox-dev \
        ${NVCC_PKG} \
    && rm -rf /var/lib/apt/lists/*

# wheel is not optional here. openai-whisper installs from an sdist with
# --no-build-isolation (so the sdist build sees the CUDA torch already in
# this venv rather than resolving its own), and that flag also stops pip
# fetching build dependencies -- so setuptools and wheel have to be present
# already. python -m venv provides pip and setuptools but NOT wheel, and the
# result is `error: invalid command 'bdist_wheel'` forty seconds into a
# twenty-minute build.
#
# setuptools is pinned below 81 for the other half of the same problem.
# whisper's setup.py imports pkg_resources, which setuptools 81 removed, so
# an unpinned --upgrade traded 'invalid command bdist_wheel' for
# 'ModuleNotFoundError: No module named pkg_resources'. The package is from
# November 2023 and expects the setuptools of its era.
RUN python3.10 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir "setuptools<81" wheel

# --recursive is load-bearing: Matcha-TTS is a submodule and the package does
# not import without it. runpod_start.sh runs `git submodule update` a second
# time to survive a flaky network; pinning the commit here does the same job
# permanently, and also stops a rebuild silently picking up upstream changes.
# Pinned to a commit, not a branch. This is living code against a model that
# shipped recently: `main` moving under a rebuild would change the server, the
# vLLM adapter and the export format at once, with the only symptom being audio
# that sounds slightly different.
ARG COSYVOICE_REF=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
RUN git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git /opt/cosyvoice \
 && cd /opt/cosyvoice \
 && git checkout "${COSYVOICE_REF}" \
 && git submodule update --init --recursive

WORKDIR /opt/cosyvoice

# Same trimming as runpod_start.sh, and for the same reasons. torch and numpy
# are pinned separately below so the resolver cannot walk the CUDA build off.
# tensorrt builds a ~3.1GB wheel for an inference path this server does not use.
RUN cp requirements.txt requirements.txt.orig \
 && sed -i -E '/^torch[=<>~]/d;/^torchaudio[=<>~]/d;/^torchvision[=<>~]/d;/^numpy[=<>~]/d' requirements.txt \
 && sed -i -E '/^tensorrt/d' requirements.txt

# Who owns the torch version depends on the variant, and getting this backwards
# is how the Voxtral image spent 18GB per boot: vLLM pins torch EXACTLY
# (0.11.0 -> 2.8.0), so installing our own torch first only means pip discards
# it and downloads another. When vLLM is in play it resolves torch; otherwise we
# pin it ourselves against the cu121 index.
RUN if [ -n "$VLLM_SPEC" ]; then \
        echo ">>> vLLM build: letting $VLLM_SPEC resolve torch"; \
        /opt/venv/bin/pip install --no-cache-dir "$VLLM_SPEC" "$TRANSFORMERS_SPEC" "numpy==1.26.4"; \
    else \
        echo ">>> cu121 build: pinning torch explicitly"; \
        /opt/venv/bin/pip install --no-cache-dir ${TORCH_SPEC} --index-url "$TORCH_INDEX"; \
    fi

# Ahead of requirements.txt on purpose: whisper builds from an sdist, and doing
# it here keeps that build isolated from the resolver churn that follows.
RUN /opt/venv/bin/pip install --no-cache-dir openai-whisper==20231117 --no-build-isolation

# fastapi/uvicorn/python-multipart named explicitly so the service still starts
# if upstream ever drops them from requirements.txt.
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
        "numpy==1.26.4" "markupsafe==2.1.5" "pillow<11.0" \
        python-multipart fastapi uvicorn soundfile hf_transfer

# requirements.txt asks for transformers==4.51.3, which vLLM 0.11 does not want.
# The strip above removes torch and numpy but not transformers, so the pins are
# re-asserted here rather than left to whichever install ran last. vllm itself
# is already satisfied, so this is a no-op for it.
RUN if [ -n "$VLLM_SPEC" ]; then \
        /opt/venv/bin/pip install --no-cache-dir "$VLLM_SPEC" "$TRANSFORMERS_SPEC" "numpy==1.26.4"; \
    fi

# ttsfrd is the better text frontend; wetext is the fallback and the server runs
# either way, so this must not fail the build. It needs only the resource zip
# and two wheels, both cp310.
RUN /opt/venv/bin/python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('FunAudioLLM/CosyVoice-ttsfrd', local_dir='pretrained_models/CosyVoice-ttsfrd')" \
 && cd pretrained_models/CosyVoice-ttsfrd \
 && unzip -o resource.zip -d . \
 && /opt/venv/bin/pip install --no-cache-dir ./ttsfrd_dependency-0.1-py3-none-any.whl \
 && /opt/venv/bin/pip install --no-cache-dir ./ttsfrd-0.4.2-cp310-cp310-linux_x86_64.whl \
 || echo ">>> ttsfrd unavailable; wetext will be used (not fatal)"

# --- the model weights ------------------------------------------------------
#
# Pinned to a revision, not to "main", for the same reason core/languages.py
# pins the MFA acoustic model per language: the weights decide what the
# narrator sounds like, and every book in the library was narrated by one
# particular set of them. A float here would re-voice the catalogue silently on
# the next rebuild.
ARG COSYVOICE_MODEL_REVISION=29e01c4e8d000f4bcd70751be16fa94bf3d85a18
ENV COSYVOICE_MODEL_REVISION=${COSYVOICE_MODEL_REVISION}

COPY docker/cosyvoice/weights.sh /usr/local/bin/cosyvoice-weights

# Whether the 5.42GB of weights travel IN the image. Default 1, which is :3.3
# and is what a production image should be. 0 leaves `cosyvoice-weights ensure`
# to fetch them at boot -- the fallback that has existed since the bake did --
# and exists because a GitHub runner has 50GB and the vLLM variant does not fit
# in it with them.
ARG BAKE_WEIGHTS=1

# Three layers rather than one, and the split is by size rather than by
# meaning: Docker pulls three layers concurrently by default, so 5.42GB split
# 2.02 / 1.41 / 1.99 reaches a cold host faster than one 5.42GB blob.
RUN chmod +x /usr/local/bin/cosyvoice-weights \
 && if [ "$BAKE_WEIGHTS" = "1" ]; then cosyvoice-weights fetch llm.pt; fi
RUN if [ "$BAKE_WEIGHTS" = "1" ]; then cosyvoice-weights fetch flow.pt hift.pt; fi
RUN if [ "$BAKE_WEIGHTS" = "1" ]; then \
        cosyvoice-weights fetch cosyvoice3.yaml config.json configuration.json \
            campplus.onnx speech_tokenizer_v3.onnx "CosyVoice-BlankEN/*"; \
    fi

# Assert the bake, here, where failing is free. Without this the image still
# boots -- `ensure` would download at runtime -- so a partial or silently
# skipped bake would look exactly like a working image while costing GPU
# minutes on every pod, which is the whole thing this layer exists to remove.
RUN if [ "$BAKE_WEIGHTS" = "1" ]; then cosyvoice-weights check; \
    else echo ">>> weights NOT baked: pods will fetch them at boot"; fi

# Bake the vLLM checkpoint too, for the same reason and with the same shape:
# it is written once and then skipped forever, so the only question is whether
# a free runner writes it or twelve billing pods each do.
#
# Deliberately NOT fatal. Upstream exports on first load, so an image without
# this still serves -- it just pays the conversion on a GPU once per pod. The
# banner is the point: "quietly slower forever" is the failure this could have.
COPY docker/cosyvoice/export_vllm.py /usr/local/bin/cosyvoice-export-vllm
RUN if [ -n "$VLLM_SPEC" ] && [ "$BAKE_WEIGHTS" = "1" ]; then \
        /opt/venv/bin/python /usr/local/bin/cosyvoice-export-vllm \
        || echo ">>> vLLM EXPORT NOT BAKED -- pods will convert at first boot"; \
    else echo ">>> vLLM export skipped (no weights in the image to convert)"; fi

# --- Montreal Forced Aligner ------------------------------------------------
#
# Alignment runs on the pod that generated the audio, not on the reader's Mac.
# MFA publishes an x86 image only, so on Apple Silicon it runs under Rosetta in
# Docker Desktop's 4GB VM at 14.7x realtime: 65 minutes for a sixteen-hour book,
# against about 25 to generate it. Parallelising it there makes it WORSE --
# `--single_speaker --num_jobs 6` measured 1,822s against 118s for the same 24
# chunks, six emulated processes thrashing. This pod is native x86 with a dozen
# idle cores while the GPU works, and it already holds the audio.
#
# PINNED, not "latest", and to the version the Mac was running (the image
# reports 3.3.10.dev0). The acoustic model decides where word boundaries land
# and core/languages.py pins that per language for exactly this reason; letting
# the *aligner* float would reintroduce the same drift from the other side.
# conda-forge's latest is 3.4.2 and is deliberately not what this takes.
#
# Its own environment rather than the serving venv. MFA is a conda package with
# compiled kaldi and openfst binaries and its own numpy/scipy constraints;
# resolving it alongside CosyVoice's CUDA torch stack is how you find out at
# serve time that something quietly upgraded numpy to 2.x. Separate prefixes
# cannot fight, and MFA is invoked as a subprocess anyway.
ARG MFA_VERSION=3.3.10
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest \
      | tar -xj -C /tmp bin/micromamba \
 && /tmp/bin/micromamba create -y -p /opt/mfa -c conda-forge \
        "montreal-forced-aligner=${MFA_VERSION}" \
 && /tmp/bin/micromamba clean -a -y \
 && rm -rf /tmp/bin/micromamba /opt/mfa/pkgs

# Prove the aligner can actually align, here, on a free runner.
#
# `mfa version` alone is NOT that proof, and this is the second time this
# project has learned that a check which does not exercise the failure path
# passes on something broken. Measured on a real pod: version answered fine,
# and every alignment died in four seconds with
#
#     ThirdpartyError: Could not find 'fstcompile'
#
# because MFA shells out to the OpenFST and Kaldi binaries and finds them on
# PATH -- which, for a server started from a different environment, does not
# include /opt/mfa/bin. So the binaries themselves are asserted, and the CLI is
# run the way the server runs it: by absolute path, with a PATH that does not
# mention the aligner, exactly as custom_api.py's _mfa_env has to repair.
RUN /opt/mfa/bin/mfa version \
 && for b in fstcompile fstarcsort gmm-align-compiled feat-to-dim; do \
        test -x "/opt/mfa/bin/$b" || { echo "MISSING /opt/mfa/bin/$b"; exit 1; }; \
    done \
 && echo "openfst + kaldi binaries present"

# And prove the third-party check itself passes, which is the exact code path
# that failed. `mfa align` runs initialize_configuration -> check_third_party
# before it looks at its arguments, so pointing it at paths that do not exist
# is enough: a healthy image complains about the missing corpus, a broken one
# raises ThirdpartyError first. No dictionary, no acoustic model, no download --
# a couple of seconds on a free runner for the one failure that otherwise costs
# a pod, a boot and a book's worth of generation to discover.
RUN set -eu; \
    out="$(PATH=/opt/mfa/bin:$PATH /opt/mfa/bin/mfa align \
             /nonexistent/corpus /nonexistent/dict /nonexistent/model \
             /tmp/nonexistent-out 2>&1 || true)"; \
    case "$out" in \
      *ThirdpartyError*|*fstcompile*) \
        echo "$out" | tail -20; \
        echo ">>> the aligner cannot reach its OpenFST/Kaldi binaries"; exit 1 ;; \
    esac; \
    echo "mfa align gets past check_third_party"

COPY server/custom_api.py /opt/cosyvoice/custom_api.py
COPY server/idle_watchdog.py /opt/cosyvoice/idle_watchdog.py

# No directory named tests/ is deleted to shrink this image, and that is a rule
# rather than an oversight: numpy ships numpy/_core/tests as a real imported
# subpackage, scipy.linalg pulls it in, and deleting it produced an image that
# could not import its own stack. Caught on a free runner by exactly the check
# below, which is why the check exists.

# Compile against Python.h rather than asking whether cc exists. "Is a compiler
# installed" passes on an image that cannot build anything, and both failures
# this guards against surface only after a pod has pulled the image and loaded
# the model -- i.e. on a billing GPU instead of here.
# The exact path the runtime failure named. An import check cannot catch this
# -- transformers only probes for nvcc when a GPU is present, so the check
# passes on a runner and fails on a billing pod. Asserting the file exists is
# the only form of this test that works where the build runs.
RUN test -x /usr/local/cuda/bin/nvcc \
 && /usr/local/cuda/bin/nvcc --version | tail -2 \
 && echo "nvcc present"

RUN printf '#include <Python.h>\nint main(void){return 0;}\n' > /tmp/hdrcheck.c \
 && cc -I"$(/opt/venv/bin/python -c 'import sysconfig;print(sysconfig.get_paths()["include"])')" \
       -c /tmp/hdrcheck.c -o /tmp/hdrcheck.o \
 && rm -f /tmp/hdrcheck.c /tmp/hdrcheck.o \
 && echo "C compiler + Python headers OK"

# Import the whole stack, including the server module, without a GPU. Importing
# custom_api is the point: it is what proves the image can actually serve rather
# than merely that its dependencies resolved.
# The version the build is supposed to have produced, asserted rather than
# hoped for. pip resolves the stack four separate times in this file and the
# last install wins; a silent downgrade of transformers or numpy is exactly the
# class of thing that surfaces as a 500 on a billing pod.
ARG EXPECT_TORCH=2.3

RUN EXPECT_TORCH="$EXPECT_TORCH" VLLM_SPEC="$VLLM_SPEC" /opt/venv/bin/python -c "\
import os, numpy, scipy.linalg, torch, torchaudio, transformers, fastapi, uvicorn; \
import cosyvoice; from cosyvoice.cli.cosyvoice import AutoModel; \
want = os.environ['EXPECT_TORCH']; \
assert torch.__version__.startswith(want), (torch.__version__, want); \
assert numpy.__version__.startswith('1.26'), numpy.__version__; \
print('numpy', numpy.__version__, '| torch', torch.__version__, \
      '| torchaudio', torchaudio.__version__, '| transformers', transformers.__version__)"

# Which CUDA this image was built against, which is what decides the GPU
# architectures it can run. Measured the hard way on 2026-09-22: an RTX PRO
# 4500 (sm_120) pulled the image, loaded the model, answered /health with ok,
# and then failed every request with "no kernel image is available for
# execution on the device".
#
# The obvious probe -- torch.cuda.get_arch_list() -- does NOT work here, and
# that is worth writing down because the first version of this check printed
# "Blackwell: NO" on a perfectly good cu128 image. Without libcuda the list
# comes back empty, so the check reported the runner's lack of a GPU as a
# property of the image. torch.version.cuda needs no driver and is the fact
# that actually determines the answer: cu121 covers sm_50..sm_90, cu128 adds
# sm_100 and sm_120.
ARG EXPECT_CUDA=12.1

RUN EXPECT_CUDA="$EXPECT_CUDA" /opt/venv/bin/python -c "\
import os, torch; \
cu, want = torch.version.cuda, os.environ['EXPECT_CUDA']; \
assert cu.startswith(want), (cu, want); \
archs = torch.cuda.get_arch_list(); \
print('torch CUDA', cu, '| compiled archs:', \
      ' '.join(archs) if archs else '(unreadable without a CUDA driver, which is not a verdict)')"

# Prove the vLLM path imports and that the custom architecture registers, on a
# CPU runner. This is the step that would have caught upstream moving the
# adapter, or vLLM renaming ModelRegistry -- neither of which /health can see.
RUN if [ -n "$VLLM_SPEC" ]; then \
        /opt/venv/bin/python -c "\
import vllm; \
from vllm import ModelRegistry; \
from cosyvoice.vllm.cosyvoice2 import CosyVoice2ForCausalLM; \
ModelRegistry.register_model('CosyVoice2ForCausalLM', CosyVoice2ForCausalLM); \
print('vllm', vllm.__version__, '| CosyVoice2ForCausalLM registered')"; \
    fi

EXPOSE 8000

# API_WORKERS stays 1 by default: one server worker with two callers is the only
# configuration whose audio has been accepted by ear. Raising it is SAC-40's
# experiment and needs a listening test, not a stopwatch -- but it is an env var
# here so that experiment needs no rebuild.
# MFA_BIN is what server/custom_api.py probes to decide whether this pod can
# align. Stated here rather than left to the server's default so that an image
# built without the aligner answers /align with 404 and the engine falls back
# to the Mac, instead of failing a book at the last step.
ENV API_WORKERS=1 PORT=8000 MFA_BIN=/opt/mfa/bin/mfa
# The vLLM variant's serving settings, baked in because the public template
# carries none: on this image LOAD_VLLM=0 babbles (no words at all). The values
# are the ones every :4.0 book was narrated with, from the old template; a
# template or the engine's deploy env still overrides them. cu121 keeps 0/1.
ARG LOAD_VLLM=0
ARG GEN_CONCURRENCY=1
ARG VLLM_GPU_UTIL=
ENV LOAD_VLLM=${LOAD_VLLM} GEN_CONCURRENCY=${GEN_CONCURRENCY} \
    VLLM_GPU_UTIL=${VLLM_GPU_UTIL}
# The start the RunPod template used to paste in (a log page on 8001, the
# weights, the API), so no template needs a start command any more.
COPY docker/cosyvoice/start.sh /usr/local/bin/cosyvoice-start
CMD ["/usr/local/bin/cosyvoice-start"]
