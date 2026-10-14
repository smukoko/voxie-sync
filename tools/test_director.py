#!/usr/bin/env python3
"""The narration director: JEV's choice -> CosyVoice 3's own controls, end to end.

Offline: JEV and the server are fakes. What is asserted is everything that can
go wrong quietly:
  - a delivery mapped to an instruction or tag the model was never trained on
  - a French book losing its accent to an instruction (measured 2026-10-06)
  - a delivery that strays from the baseline's one native control each
  - a chunk spanning two deliveries, or a directed take served from a plain cache entry
  - "[breath]" reaching the alignment transcript
  - an older server, or a registered speaker, silently dropping the instruction
  - the free route (Kokoro, no key) being directed at all

    python tools/test_director.py
"""
import ast
import asyncio
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import director  # noqa: E402
from core.director import (DELIVERIES, INSTRUCTIONS, TAGS, Direction, PLAIN,  # noqa: E402
                           direct_blocks, direction_for, group_by_direction)
from core.tts_worker import cache_text  # noqa: E402
from core import tts_providers  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail and not ok else ""))


# --- 1. The menu only uses what the model knows ---------------------------------
# The baseline's eight, one native control each (user, 2026-10-12): the
# sixteen composed with speed changes and sighs sounded less natural.
check("the baseline's eight deliveries",
      list(DELIVERIES) == ["plain", "joyful", "sorrowful", "angry", "hushed", "frantic", "shouted", "heavy"],
      list(DELIVERIES))
check("each maps to one native instruction, never two deliveries to one, plain to none",
      DELIVERIES["plain"].instruct is None
      and sorted(d.instruct for k, d in DELIVERIES.items() if k != "plain") == sorted(INSTRUCTIONS))
check("every instruction is in CosyVoice 3's trained form",
      all(i.startswith("You are a helpful assistant. ") and i.endswith("<|endofprompt|>")
          for i in INSTRUCTIONS.values()))
check("every delivery maps to a known instruction and tag, and no speed is ever changed",
      all((d.instruct is None or d.instruct in INSTRUCTIONS) and (d.tag == "" or d.tag in TAGS)
          for d in DELIVERIES.values())
      and all(direction_for(k, "en", p).speed == 1.0 for k in DELIVERIES for p in (0.0, 0.9)))
check("JEV is asked only the baseline's two questions", set(director.QUESTIONS) == {"delivery", "breath_before"})
check("JEV is offered exactly the menu",
      set(director.QUESTIONS["delivery"]["criteria"]) == set(DELIVERIES))

# --- 2. Composition ------------------------------------------------------------------
en = direction_for("frantic", "en")
# Speed and a breath, no instruction: what a delivery with no instruction sends.
nudge = director.Direction("exhausted", None, 0.92, "[breath]")
check("English gets the instruction", en.instruct_text == INSTRUCTIONS["fast"])
check("no sigh and no laugh is ever added", not set(TAGS) & {"[sigh]", "[laughter]"})
check("a breath only when the delivery brings no tag of its own",
      direction_for("plain", "en", p_breath=0.8).tag == "[breath]"
      and direction_for("frantic", "en", p_breath=0.8).tag == "[quick_breath]")
check("an unknown choice is plain", direction_for("ecstatic", "en") == PLAIN)
check("plain is plain", PLAIN.plain and not en.plain)


# --- 3. Asking JEV ---------------------------------------------------------------
class FakeJev:
    def __init__(self, choice="hushed", fail_on=None):
        self.calls, self.choice, self.fail_on = [], choice, fail_on

    async def decide(self, state, questions):
        self.calls.append(state)
        if self.fail_on and self.fail_on in state["line"]:
            raise RuntimeError("JEV is down")
        return {"delivery": {"choice": self.choice}, "breath_before": {"noul": 0.1}}


texts = ["Chapter One.", "He crept along the wall, holding his breath in the dark.",
         "Nobody moved in the house that night at all.", "The lamp flickered once and went out for good."]
jev = FakeJev(fail_on="Nobody")
ds = asyncio.run(direct_blocks(texts, "en", jev))
check("a heading is read plain, without asking", ds[0] == PLAIN and len(jev.calls) == 3)
check("JEV's choice is applied", ds[1].delivery == "hushed" and ds[1].instruct_text == INSTRUCTIONS["soft"])
check("an outage costs the direction, not the paragraph", ds[2] == PLAIN)
check("JEV reads the neighbours", "Chapter One." in jev.calls[0]["before"]
      and "Nobody" in jev.calls[0]["after"])

# --- 4. One delivery per chunk -----------------------------------------------------
blocks = [("a", "x"), ("b", "y"), ("c", "z"), ("d", "w")]
runs = group_by_direction(blocks, [PLAIN, en, en, PLAIN])
check("chunks break where the delivery changes",
      [len(r[1]) for r in runs] == [1, 2, 1] and [b for _, r in runs for b in r] == blocks)
check("a directed take never shares a cache entry with a plain one",
      cache_text("t", None) == "t" == cache_text("t", PLAIN) and cache_text("t", en) != "t"
      and cache_text("t", en) != cache_text("t", direction_for("hushed", "en")))

# --- 5. Who is directed ------------------------------------------------------------
key = {"OPENROUTER_API_KEY": "k"}
check("the free route is never directed", director.mode(tts_providers.KokoroProvider, key) == "off")
check("CosyVoice with a key is directed", director.mode(tts_providers.CosyVoiceProvider, key) == "on")
check("no key: quietly off", director.mode(tts_providers.CosyVoiceProvider, {}) == "off")
check("NARRATION_DIRECTOR=off is off",
      director.mode(tts_providers.CosyVoiceProvider, {**key, "NARRATION_DIRECTOR": "off"}) == "off")
try:
    director.mode(tts_providers.CosyVoiceProvider, {"NARRATION_DIRECTOR": "jev"})
    check("NARRATION_DIRECTOR=jev without a key fails loudly", False)
except RuntimeError:
    check("NARRATION_DIRECTOR=jev without a key fails loudly", True)

# --- 6. The server keeps tags out of alignment ---------------------------------------
src = open(os.path.join(ROOT, "server", "custom_api.py"), encoding="utf-8").read()
pattern = next(n.value.args[0].value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_TAG_RE")
tag_re = re.compile(pattern)
check("every tag the director can emit is stripped from the kept transcript",
      all(tag_re.sub("", t + "He ran.") == "He ran." for t in TAGS))
check("words in brackets that are not tags stay", tag_re.sub("", "[Note] He ran.") == "[Note] He ran.")
check("the server advertises direction", '"features": ["direction"]' in src)
check("the server sends instructions only with the clip itself",
      "inference_instruct2" in src and "instruct_text needs audio_file" in src)


# --- 7. The provider -------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeServer:
    def __init__(self, features):
        self.features, self.posts = features, []

    async def get(self, url, timeout=None):
        return Resp(200, {"status": "ok", "features": self.features})

    async def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[1], dict(data or {}), bool(files)))
        return Resp(200)


tmp = tempfile.mkdtemp()
wav = os.path.join(tmp, "ref.wav")
open(wav, "wb").write(b"RIFF" + b"\0" * 2000)
os.environ["COSY_REF_WAV"], os.environ["COSY_REF_TEXT"] = wav, "Exact words of the clip."
prov = tts_providers.CosyVoiceProvider(language="en")


def last_clone(server, direction, text="He ran."):
    asyncio.run(prov.synthesize(server, "http://pod", text, 30, direction=direction))
    return [p for p in server.posts if p[0] == "clone"][-1]


new = FakeServer(["direction"])
_, data, uploaded = last_clone(new, en)
check("an instruction travels with the clip, not the registered speaker",
      uploaded and data["instruct_text"] == INSTRUCTIONS["fast"] and "speaker_id" not in data)
check("the tag goes in front of the text, at the model's own speed",
      data["text"] == "[quick_breath]He ran." and data["speed"] == "1.0")
_, data, uploaded = last_clone(new, nudge)
check("speed and breath alone keep the registered speaker",
      not uploaded and data.get("speaker_id") and "instruct_text" not in data and data["text"].startswith("[breath]"))
_, data, _ = last_clone(new, None)
check("an undirected chunk is unchanged", data["text"] == "He ran." and "speed" not in data)
prov2 = tts_providers.CosyVoiceProvider(language="en")
prov = prov2
old = FakeServer([])
_, data, _ = last_clone(old, en)
check("an older server gets the plain chunk: no tag, no instruction, no speed",
      data["text"] == "He ran." and "instruct_text" not in data and "speed" not in data)

failed = results.count(False)
print("=" * 60)
print(f"{len(results)} checks: {len(results) - failed} passed, {failed} FAILED")
sys.exit(1 if failed else 0)
