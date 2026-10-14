#!/usr/bin/env python3
"""The junk filter's seam: who decided, and what happens when they disagree.

Runs with no network, no API key and no ML stack -- the providers under test
are fakes, because what is being tested is the seam, not anybody's accuracy.

The properties that matter are the ones that make a shadow run safe to leave
on for a whole book:

  - the primary's verdict is what the engine gets, always
  - a shadow that raises cannot take the book down with it
  - a disagreement is recorded with enough context to argue about later

    python tools/test_junk_provider.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.junk_provider import (  # noqa: E402
    JUNK, KEEP, FallbackProvider, JunkProvider, ShadowProvider, Verdict, build, get_junk_provider,
)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


class Fake(JunkProvider):
    def __init__(self, name, verdict, confidence=None, boom=False):
        self.name, self._v, self._c, self._boom = name, verdict, confidence, boom
        self.calls = 0
        self.closed = False

    async def classify(self, text, word_count, position_ratio, identifier=""):
        self.calls += 1
        if self._boom:
            raise RuntimeError("shadow exploded")
        return Verdict(verdict=self._v, provider=self.name, confidence=self._c)

    async def aclose(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


# --- selection -------------------------------------------------------------
os.environ.pop("JUNK_PROVIDER", None)
check("the default provider is the incumbent", get_junk_provider().name == "ai_brain")

os.environ["JUNK_PROVIDER"] = "jev"
p = get_junk_provider()
check("jev is selectable by name, backed by the incumbent",
      isinstance(p, FallbackProvider) and p.primary.name == "jev" and p.fallback_name == "ai_brain",
      getattr(p, "display", p.name))
os.environ["JUNK_FALLBACK"] = "none"
check("the fallback can be turned off", get_junk_provider().name == "jev")
os.environ.pop("JUNK_FALLBACK")

# The 2026-09-25 outage: the key hit its monthly limit and every call was a 403.
fb = FallbackProvider(Fake("jev", JUNK, boom=True), "ai_brain")
backup = Fake("ai_brain", KEEP)
fb.fallback = backup
v = run(fb.classify("text", 100, 0.5, "ch1"))
check("a JEV outage costs the verdict, not the book", v.verdict == KEEP and v.provider == "ai_brain", str(v))
run(fb.classify("text", 100, 0.5, "ch2"))
check("...every document after it is decided too, and counted",
      fb.failures == 2 and backup.calls == 2 and "2 document(s)" in fb.summary(), fb.summary())
healthy = FallbackProvider(Fake("jev", JUNK, 0.97), "ai_brain")
v = run(healthy.classify("text", 100, 0.5, "ch1"))
check("a healthy primary decides and the fallback is never built",
      v.provider == "jev" and healthy.fallback is None)
run(fb.aclose())
check("closing closes both", backup.closed)

os.environ["JUNK_PROVIDER"] = "nope"
try:
    get_junk_provider()
    check("an unknown provider is refused", False)
except ValueError as exc:
    check("an unknown provider is refused", "nope" in str(exc))

os.environ["JUNK_PROVIDER"] = "shadow"
os.environ["JUNK_PRIMARY"] = os.environ["JUNK_SHADOW"] = "ai_brain"
try:
    get_junk_provider()
    check("shadowing a provider with itself is refused", False)
except ValueError:
    check("shadowing a provider with itself is refused", True)
finally:
    for k in ("JUNK_PROVIDER", "JUNK_PRIMARY", "JUNK_SHADOW"):
        os.environ.pop(k, None)

# --- jev: transport, key, parsing, retries --------------------------------
# Driven through httpx.MockTransport, so no key, no network and no cost. What
# is under test is this side of the wire: what we send, and what we make of
# every kind of answer that can come back.
import httpx  # noqa: E402
import core.junk_provider as jp  # noqa: E402

for k in ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY", "TYPESAFE_API_KEY", "JEV_TRANSPORT", "JEV_MODEL"):
    os.environ.pop(k, None)

j = build("jev")
check("jev defaults to OpenRouter", j.transport == "openrouter" and "openrouter.ai/api/alpha/decisions" in j.url)
check("the model is pinned, not floating", j.model == "typesafe/jev-1.13", j.model)

try:
    j._api_key()
    check("a missing key is refused by name", False)
except RuntimeError as exc:
    check("a missing key is refused by name",
          "OPENROUTER_API_KEY" in str(exc) and "OPEN_ROUTER_API_KEY" in str(exc), str(exc)[:70])

os.environ["OPEN_ROUTER_API_KEY"] = "test-key"
check("either spelling of the OpenRouter key is accepted", j._api_key() == "test-key")

try:
    jp.JevProvider(transport="carrier-pigeon")
    check("an unknown transport is refused", False)
except ValueError:
    check("an unknown transport is refused", True)


def mocked(handler):
    """A JevProvider whose HTTP goes to `handler` instead of the internet."""
    p = jp.JevProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p._sem = asyncio.Semaphore(4)
    return p


def answer(noul, kind="narrative", cost=0.000024, model="typesafe/jev-1.13-20260917"):
    return httpx.Response(200, json={
        "model": model,
        "answers": {
            "is_not_the_author": {"type": "noul", "noul": noul},
            "section_kind": {"type": "choice", "choice": kind, "confidence": 0.9,
                             "probabilities": {kind: 0.95}},
        },
        "usage": {"input_tokens": 500, "output_tokens": 40, "cost": cost},
    })


sent = []
def good(request):
    sent.append(json.loads(request.content))
    return answer(0.98, "front_matter")

p = mocked(good)
v = run(p.classify("Copyright 1997. All rights reserved.", 12, 0.01, "cover.xhtml"))
check("a confident noul becomes JUNK", v.verdict == JUNK and v.confidence == 0.98, str(v))
check("the section kind travels as evidence", v.detail == "kind=front_matter", v.detail)
body = sent[0]
check("the request carries model, state and questions",
      body["model"] == "typesafe/jev-1.13" and body["state"]["text"].startswith("Copyright")
      and set(body["questions"]) == {"is_not_the_author", "section_kind"})
check("instructions point at the state field in backticks",
      "`text`" in body["questions"]["is_not_the_author"]["instructions"])
check("cost and the served model are recorded",
      abs(p.cost - 0.000024) < 1e-12 and "typesafe/jev-1.13-20260917" in p.served_models, p.summary())

p = mocked(lambda r: answer(0.02))
check("a low noul stays KEEP", run(p.classify("She ran.", 2, 0.5)).verdict == KEEP)

p = mocked(lambda r: answer(0.84))
check("just under the asymmetric threshold is kept", run(p.classify("x", 1, 0.5)).verdict == KEEP)

# Retries: sleep is patched out so the backoff costs nothing here.
_real_sleep = jp.asyncio.sleep
async def _no_sleep(_):
    return None
jp.asyncio.sleep = _no_sleep
try:
    tries = []
    def flaky(request):
        tries.append(1)
        return httpx.Response(520, text="origin error") if len(tries) < 3 else answer(0.01)
    p = mocked(flaky)
    v = run(p.classify("She ran.", 2, 0.5))
    check("a 5xx is retried until it succeeds", v.verdict == KEEP and len(tries) == 3, f"{len(tries)} attempts")

    tries = []
    def throttled(request):
        tries.append(1)
        return httpx.Response(429, text="slow down") if len(tries) < 2 else answer(0.01)
    run(mocked(throttled).classify("x", 1, 0.5))
    check("a 429 is retried", len(tries) == 2, f"{len(tries)} attempts")

    tries = []
    def rejected(request):
        tries.append(1)
        return httpx.Response(400, json={"error": {"message": "bad question"}})
    try:
        run(mocked(rejected).classify("x", 1, 0.5))
        check("a 4xx is raised at once, not retried", False)
    except RuntimeError as exc:
        check("a 4xx is raised at once, not retried", len(tries) == 1 and "400" in str(exc), f"{len(tries)} attempt(s)")

    tries = []
    def dead(request):
        tries.append(1)
        return httpx.Response(503)
    try:
        run(mocked(dead).classify("x", 1, 0.5))
        check("a provider that never recovers fails loudly", False)
    except RuntimeError as exc:
        check("a provider that never recovers fails loudly",
              len(tries) == jp.JEV_ATTEMPTS and "attempts" in str(exc), f"{len(tries)} attempts")
finally:
    jp.asyncio.sleep = _real_sleep
    os.environ.pop("OPEN_ROUTER_API_KEY", None)

# --- the shadow must never change the book ---------------------------------
with tempfile.TemporaryDirectory() as tmp:
    log = os.path.join(tmp, "shadow.jsonl")

    primary, shadow = Fake("primary", KEEP), Fake("shadow", JUNK, 0.99)
    sp = ShadowProvider(primary, shadow, log_path=log)
    got = run(sp.classify("The author's own words.", 900, 0.4, "ch3.xhtml"))

    check("the engine gets the PRIMARY's verdict", got.verdict == KEEP, str(got))
    check("both providers were actually asked", primary.calls == 1 and shadow.calls == 1)
    check("the disagreement was counted",
          len(sp.disagreements) == 1 and sp.agreements == 0)

    row = json.loads(open(log).read().strip())
    check("the log names both verdicts",
          row["primary"]["verdict"] == KEEP and row["shadow"]["verdict"] == JUNK)
    check("the log keeps the evidence",
          row["identifier"] == "ch3.xhtml" and "author's own words" in row["snippet"])
    check("the log keeps the shadow's confidence", row["shadow"]["confidence"] == 0.99)

    # Agreement is silent and costs nothing but the tally.
    sp2 = ShadowProvider(Fake("primary", JUNK), Fake("shadow", JUNK), log_path=log)
    run(sp2.classify("Copyright 1996.", 12, 0.02, "cover.xhtml"))
    check("agreement is recorded, not logged",
          sp2.agreements == 1 and sp2.disagreements == [])
    check("the summary reports the rate", "1/1" in sp2.summary(), sp2.summary())

    # The whole reason the shadow is running is that nobody trusts it yet.
    boom = Fake("shadow", JUNK, boom=True)
    sp3 = ShadowProvider(Fake("primary", KEEP), boom, log_path=log)
    got = run(sp3.classify("Chapter One.", 500, 0.3, "ch1.xhtml"))
    check("a shadow that raises cannot fail the book", got.verdict == KEEP, str(got))

    p4, s4 = Fake("primary", KEEP), Fake("shadow", KEEP)
    run(ShadowProvider(p4, s4, log_path=log).aclose())
    check("closing the shadow closes both clients", p4.closed and s4.closed)

# --- the verdict carries its own provenance --------------------------------
v = Verdict(JUNK, "jev", 0.97, {"legal": 0.9}, "kind=legal", 88.1)
check("a verdict says who decided and how sure", str(v) == "JUNK(jev p=0.97)", str(v))
check("a provider with no calibration does not invent one",
      Verdict(KEEP, "ai_brain").confidence is None)

# --- what a book spent on OpenRouter, through every wrapper -------------------
from types import SimpleNamespace  # noqa: E402
from core.junk_provider import spend  # noqa: E402
jev_a, jev_b, local = SimpleNamespace(cost=0.10), SimpleNamespace(cost=0.03), SimpleNamespace()
shadowed = SimpleNamespace(primary=local, shadow=jev_a)
fallen = SimpleNamespace(cost=0.0, primary=SimpleNamespace(cost=0.02), fallback=jev_b)
check("spend adds up a shadow's and a fallback's calls, and the plain ones",
      abs(spend(shadowed) + spend(fallen) + spend(SimpleNamespace(cost=0.14)) - 0.29) < 1e-9,
      f"{spend(shadowed) + spend(fallen) + spend(SimpleNamespace(cost=0.14)):.4f}")
check("no provider (the director off) spent nothing", spend(None) == 0.0)
check("a local filter with no cost at all spent nothing", spend(local) == 0.0)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
