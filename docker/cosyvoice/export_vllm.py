"""Bake the vLLM checkpoint at BUILD time, for the same reason as the weights.

`CosyVoice3Model.load_vllm()` calls `export_cosyvoice2_vllm()`, which rewrites
the LLM as a Qwen2-style checkpoint under `{model_dir}/vllm` with
`save_pretrained` -- and returns immediately if that directory already exists.
So the conversion happens exactly once, and the only question is whether it
happens here for free or on every pod at $1.09/hr.

It can happen here because nothing in the path needs a GPU: CosyVoiceModel
picks its device with `torch.device('cuda' if torch.cuda.is_available() else
'cpu')` rather than asserting, and the export is a dtype cast plus a
`save_pretrained`. Only the TensorRT path asserts CUDA, and this is not it.

The model is reconstructed from the yaml rather than through AutoModel, which
would build the flow decoder and vocoder as well -- three gigabytes of weights
loaded to write out one. Only `configs['llm']` is touched, which is precisely
what `CosyVoice3Model.__init__` passes as its first positional argument.

Not fatal if it fails: upstream exports at first load, so an image without this
still serves, just slower on its first boot. It prints loudly either way,
because "slower forever" is otherwise invisible.
"""
import os
import sys

sys.path.insert(0, "/opt/cosyvoice")
sys.path.insert(0, "/opt/cosyvoice/third_party/Matcha-TTS")

import torch  # noqa: E402
from hyperpyyaml import load_hyperpyyaml  # noqa: E402

from cosyvoice.utils.file_utils import export_cosyvoice2_vllm  # noqa: E402

model_dir = os.environ["COSYVOICE_MODEL_DIR"]
out_dir = os.path.join(model_dir, "vllm")

if os.path.isdir(out_dir) and os.listdir(out_dir):
    print(f">>> vLLM checkpoint already present at {out_dir}")
    sys.exit(0)

yaml_path = os.path.join(model_dir, "cosyvoice3.yaml")
with open(yaml_path, "r") as f:
    configs = load_hyperpyyaml(
        f, overrides={"qwen_pretrain_path": os.path.join(model_dir, "CosyVoice-BlankEN")})

llm = configs["llm"]
llm.load_state_dict(
    torch.load(os.path.join(model_dir, "llm.pt"), map_location="cpu", weights_only=True),
    strict=True)

export_cosyvoice2_vllm(llm, out_dir, torch.device("cpu"))

files = sorted(os.listdir(out_dir))
size = sum(os.path.getsize(os.path.join(out_dir, f)) for f in files) / 1e9
print(f">>> vLLM checkpoint baked: {len(files)} files, {size:.2f}GB in {out_dir}")
print("    " + ", ".join(files))
