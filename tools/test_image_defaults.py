"""The vLLM image starts right with no template settings at all.

The public RunPod template carries no environment (LAUNCH_PLAN 4.2-4.3), and on
the vLLM image the server's own default, LOAD_VLLM=0, babbles: no words at all
(CLAUDE.md, GPU rules). So the image itself must default to vLLM, with the
settings the private template narrated every :4.0 book with.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def vllm_build_args():
    """The build args the workflow passes for the vllm variant (the 4.3 tag)."""
    wf = read(".github/workflows/cosyvoice-image.yml")
    branch = wf.split('if [ "${{ inputs.variant }}" = "vllm" ]; then', 1)[1].split("else", 1)[0]
    # 4.2 is what the public template runs: never built again (4.3 is the
    # same image from the public repository).
    assert "tag=4.3" in branch, "the vllm branch no longer builds 4.3"
    return dict(re.findall(r"echo '([A-Z_]+)=([^']*)'", branch))


def main():
    args = vllm_build_args()
    want = {"LOAD_VLLM": "1", "GEN_CONCURRENCY": "16", "VLLM_GPU_UTIL": "0.5"}
    for k, v in want.items():
        assert args.get(k) == v, f"the vllm build passes {k}={args.get(k)!r}, want {v!r}"

    df = read("docker/cosyvoice/Dockerfile")
    for k in want:
        # Declared as an ARG and carried into the runtime ENV, or the build arg
        # is silently dropped and the pod falls back to the server's default.
        assert re.search(rf"^ARG {k}=", df, re.M), f"Dockerfile has no ARG {k}"
        assert re.search(rf"\b{k}=\$\{{{k}\}}", df), f"Dockerfile does not put {k} in ENV"
    # cu121 builds pass no args: its defaults must stay the proven ones.
    assert re.search(r"^ARG LOAD_VLLM=0$", df, re.M), "cu121 must keep LOAD_VLLM=0"
    assert re.search(r"^ARG GEN_CONCURRENCY=1$", df, re.M), "cu121 must keep GEN_CONCURRENCY=1"

    # The server reads exactly these names.
    api = read("server/custom_api.py")
    for k in want:
        assert f'os.environ.get("{k}"' in api, f"custom_api.py no longer reads {k}"
    # A tag that exists is never pushed again, and the image names its source.
    wf = read(".github/workflows/cosyvoice-image.yml")
    guard, build = wf.find("name: Refuse a tag that exists"), wf.find("name: Build and push")
    assert 0 <= guard < build, "the workflow does not refuse an existing tag before it builds"
    assert "imagetools inspect" in wf[guard:build] and "exit 1" in wf[guard:build], "the tag guard checks nothing"
    assert "org.opencontainers.image.source=https://github.com/${{ github.repository }}" in wf, \
        "the image does not name the repository that built it"
    print("PASS: the vLLM image defaults to vLLM with the template's settings")
    print("PASS: it builds a new tag, refuses an existing one, and names its source")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
