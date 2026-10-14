"""Whisper loads from the local cache: no question to the hub once the model is there.

On 2026-10-12 a narration scan hung 25 minutes on a dead connection to the hub
(CLOSE_WAIT, no timeout) before its first chunk, although the model was cached:
mlx_whisper, given a repo name, asks the hub on every call. Offline: a fake
snapshot_download and a fake mlx_whisper stand in.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from core import hf_cache  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Hub:
    """snapshot_download(local_files_only=True): a folder when cached, else the hub's error."""

    def __init__(self, cached):
        self.cached, self.asked = set(cached), []

    def __call__(self, repo, local_files_only=False):
        self.asked.append((repo, local_files_only))
        if repo not in self.cached:
            raise OSError("LocalEntryNotFoundError: not in the cache")
        return f"/cache/{repo.replace('/', '--')}/snapshot"


hub = Hub({"org/whisper"})
check("a cached model is loaded from its folder",
      hf_cache.model_path("org/whisper", hub) == "/cache/org--whisper/snapshot")
check("asking only the cache, never the hub", hub.asked == [("org/whisper", True)], f"({hub.asked})")
hf_cache.model_path("org/whisper", hub)
check("and only once", len(hub.asked) == 1)

hub = Hub(set())
check("a model not cached yet is loaded by name (the one download)", hf_cache.model_path("org/new", hub) == "org/new")
hub.cached.add("org/new")
check("once downloaded, the next call finds it in the cache",
      hf_cache.model_path("org/new", hub) == "/cache/org--new/snapshot")

saved = sys.modules.get("huggingface_hub")
sys.modules["huggingface_hub"] = None          # importing it raises ImportError
check("without huggingface_hub, by name as before", hf_cache.model_path("org/other") == "org/other")
if saved is not None:
    sys.modules["huggingface_hub"] = saved
else:
    del sys.modules["huggingface_hub"]

# --- The callers hand Whisper the cached folder ------------------------------
given = []
sys.modules["mlx_whisper"] = types.SimpleNamespace(
    transcribe=lambda path, path_or_hf_repo=None, **kw: given.append(path_or_hf_repo) or
    {"text": "", "segments": []})
import narration_scan  # noqa: E402
from libra_cli import clip_check  # noqa: E402

# Both load the same model; any folder stands for the cached one.
hf_cache._found.update({narration_scan.WHISPER: "/cache/model", clip_check.MLX_MODEL: "/cache/model"})
narration_scan.listen("chunk.wav")
narration_scan.listen("chunk.wav", guard=True)
check("the narration scan loads Whisper from the cache, both hearings",
      given == ["/cache/model", "/cache/model"], f"({given})")
given.clear()
clip_check._mlx("clip.wav", "en")
check("voice-add's clip check too", given == ["/cache/model"], f"({given})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
