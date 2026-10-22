#!/usr/bin/env python3
"""What the engine actually asks RunPod for, asserted field by field.

Every one of these is a rule that cost real money to learn and that nothing
downstream can detect when it is wrong. A pod deployed with the wrong request
still reports RUNNING and still bills; the run just never gets anything usable
out of it.

    supportPublicIp   On the TCP transport the server is reached at a raw
                      public ip:port. A host that cannot provide one rents
                      happily, reports RUNNING, publishes no portMappings, and
                      is unreachable for as long as you are willing to pay.
                      Measured 2026-09-21: three of five pods -- a 3090, an
                      L40S and a 4090 -- were exactly that, each burning the
                      full 600s port-mapping budget before being discarded.

    cloudType         ALL means "anywhere", not "cheapest". RunPod answered it
                      with secure capacity at roughly twice the community rate
                      (4090 at $0.74 against $0.34, L40S at $1.09 against
                      $0.79), on every pod of every book.

    volumeInGb: 0     A network volume is a placement constraint AND has cost
                      this project a public IP outright. The prebaked image
                      needs nothing to survive the pod.

    python tools/test_deploy_request.py
"""
import asyncio
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng   # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def capture_requests(transport, cloud="COMMUNITY"):
    """Run one sweep of the preference list and return the GraphQL bodies sent."""
    sent = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            # No capacity, so the sweep walks the whole preference list and we
            # see a request per card rather than stopping at the first.
            return {"data": {"podFindAndDeployOnDemand": None}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            sent.append(json["query"])
            return _Resp()

    real_client, real_transport, real_prices = eng.httpx.AsyncClient, eng.RUNPOD_TRANSPORT, eng.live_prices
    eng.httpx.AsyncClient = _Client
    eng.RUNPOD_TRANSPORT = transport
    eng.live_prices = no_quotes
    try:
        asyncio.run(eng._sweep_for_gpu(cloud))
    finally:
        eng.httpx.AsyncClient = real_client
        eng.RUNPOD_TRANSPORT = real_transport
        eng.live_prices = real_prices
    return sent


async def no_quotes():
    """RunPod's price list unavailable: every measured card is tried at its ceiling."""
    return {}


print("\n🎛️  TCP transport")
tcp = capture_requests("tcp")
check("a request is made for every measured card",
      len(tcp) == len(eng.cost_plan.SPEED) > 0, f"{len(tcp)} requests")
check("every deploy asks for a public IP",
      all("supportPublicIp: true" in q for q in tcp),
      f"{sum('supportPublicIp: true' in q for q in tcp)} of {len(tcp)}")
check("no network volume is requested",
      all("volumeInGb: 0" in q for q in tcp))
check("the cloud asked for is the one passed in",
      all("cloudType: COMMUNITY" in q for q in tcp))

print("\n🎛️  Proxy transport")
proxy = capture_requests("proxy")
check("the proxy transport does NOT require a public IP",
      all("supportPublicIp" not in q for q in proxy),
      "requiring one there would discard usable capacity for nothing")

print("\n🎛️  Cloud selection")
allcloud = capture_requests("tcp", "ALL")
# cloudType ALL means "anywhere" to RunPod, and bought Twisted's H100 at $3.49.
# ALL now prices both halves and deploys each card on the half it was priced on.
check("ALL deploys each card on a named cloud, never on 'anywhere'",
      allcloud and not any("cloudType: ALL" in q for q in allcloud)
      and any("cloudType: SECURE" in q for q in allcloud) and any("cloudType: COMMUNITY" in q for q in allcloud))

import subprocess  # noqa: E402
orders = {}
for transport in ("tcp", "proxy"):
    env = {**os.environ, "RUNPOD_TRANSPORT": transport}
    env.pop("RUNPOD_CLOUD_ORDER", None)
    out = subprocess.run([sys.executable, "-c", "import librerian_engine_v3 as e; print(','.join(e.CLOUD_ORDER))"],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=env,
                         capture_output=True, text=True).stdout.strip().splitlines()
    orders[transport] = out[-1] if out else ""
# Community pods on tcp: no port in 25 minutes, or a port and no answer
# (2026-09-21, and twice on 2026-10-07). "ALL" bought Twisted's H100.
check("on tcp only the secure cloud is searched by default, by name", orders["tcp"] == "SECURE", str(orders))
check("on the proxy, community first, then secure, never 'anywhere'",
      orders["proxy"] == "COMMUNITY,SECURE", str(orders))

print("\n🎛️  Server workers travel with the card")
# Each uvicorn worker holds its own copy of the model, so the safe count is a
# property of the card's VRAM: 8 fit in 48GB, and on 24GB two separate 4090s a
# day apart returned RemoteProtocolError mid-response and then stopped
# answering. Left in the template it is one number for every card.
# POD_API_WORKERS, when set, replaces the whole table -- vLLM wants one engine
# per card whatever the card is, because N processes each holding a model is
# precisely what batching replaces. So the expectation depends on the live
# environment, and asserting the per-card value regardless would fail on the
# configuration this project actually runs.
override = eng.POD_API_WORKERS
by_name = {g["name"]: g for g in eng.GPU_PREFERENCES}
for q in tcp:
    for name, gpu in by_name.items():
        if f'gpuTypeId: "{name}"' in q:
            expected = int(override) if override else gpu["api_workers"]
            want = f'key: "API_WORKERS", value: "{expected}"'
            why = " (POD_API_WORKERS override)" if override else ""
            check(f"{name.replace('NVIDIA ', '')} asks for {expected} workers{why}",
                  want in q, "not in the deploy request" if want not in q else "")

check("the override, when set, applies to every card or to none",
      not override or all(f'value: "{int(override)}"' in q for q in tcp),
      f"POD_API_WORKERS={override!r}")

check("no 24GB card is ever sent 8 server workers",
      all(g["api_workers"] <= 4 for g in eng.GPU_PREFERENCES
          if g["vram"] in ("20GB", "24GB")),
      str({g["name"]: g["api_workers"] for g in eng.GPU_PREFERENCES
           if g["vram"] in ("20GB", "24GB")}))

print("\n🎛️  The user's own token travels with every pod (LAUNCH_PLAN 4.6)")
# A public template cannot hold anyone's TTS_API_TOKEN: each engine sends its
# own, and the pod's server then accepts that user only.
real_token = eng.TTS_API_TOKEN
eng.TTS_API_TOKEN = "0123abcd" * 8
try:
    with_token = capture_requests("tcp")
finally:
    eng.TTS_API_TOKEN = real_token
check("every deploy carries this .env's TTS_API_TOKEN",
      all(f'key: "TTS_API_TOKEN", value: "{"0123abcd" * 8}"' in q for q in with_token),
      f"{sum('TTS_API_TOKEN' in q for q in with_token)} of {len(with_token)}")
check("...next to the worker count, in one env list",
      all(q.count("env: [") == 1 and "API_WORKERS" in q for q in with_token))
eng.TTS_API_TOKEN = ""
try:
    without = capture_requests("tcp")
finally:
    eng.TTS_API_TOKEN = real_token
check("no token in .env, none sent", all("TTS_API_TOKEN" not in q for q in without))
from core import pod_pool  # noqa: E402
for bad in ('x" } ], name: "evil', "a b", "tok\nen"):
    try:
        pod_pool.pod_env(1, bad)
        check(f"a token that could break the request is refused ({bad!r})", False)
    except ValueError:
        check(f"a token that could break the request is refused ({bad!r})", True)
check("nothing to send is no env field at all", pod_pool.pod_env(None, "") == "")


def server_answers(anonymous, with_token):
    """assert_server_requires_token against a pod answering /voices like this."""
    class _R:
        def __init__(self, code):
            self.status_code = code

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None, headers=None):
            return _R(with_token if headers else anonymous)

    real_client, real_tok, real_provider = eng.httpx.AsyncClient, eng.TTS_API_TOKEN, os.environ.get("TTS_PROVIDER")
    eng.httpx.AsyncClient, eng.TTS_API_TOKEN = _Client, "0123abcd" * 8
    os.environ["TTS_PROVIDER"] = "cosyvoice"
    try:
        return asyncio.run(eng.assert_server_requires_token("http://pod"))[0]
    finally:
        eng.httpx.AsyncClient, eng.TTS_API_TOKEN = real_client, real_tok
        if real_provider is None:
            os.environ.pop("TTS_PROVIDER", None)
        else:
            os.environ["TTS_PROVIDER"] = real_provider


check("a pod that refuses strangers and accepts this token is used", server_answers(401, 200) is True)
check("a pod enforcing someone else's token is refused before a chunk is sent",
      server_answers(401, 401) is False)
check("a pod enforcing no token at all is refused", server_answers(200, 200) is False)

print("\n🎛️  Which cards are rented (core/cost_plan.py)")
names = [g["name"] for g in eng.GPU_PREFERENCES]
check("every card's settings name a worker count",
      all(isinstance(g.get("workers"), int) and g["workers"] > 0
          for g in eng.GPU_PREFERENCES))
check("the settings list has no duplicates", len(names) == len(set(names)), str(names))
deployed = {q.split('gpuTypeId: "')[1].split('"')[0] for q in tcp}
check("only cards with a measured speed are ever deployed",
      deployed and deployed <= set(eng.cost_plan.SPEED), str(sorted(deployed)))


async def twisted_prices():
    """What RunPod quoted on 2026-10-07, when Twisted rented an H100."""
    quotes = {("NVIDIA H100 80GB HBM3", "SECURE"): 3.49, ("NVIDIA L40S", "SECURE"): 1.09}
    quotes.update({(g, "SECURE"): 0.20 for g in eng.cost_plan.SPEED})
    return quotes

real_prices = eng.live_prices
eng.live_prices = twisted_prices
try:
    planned = [c["name"] for c, _, _, _ in asyncio.run(eng.plan_cards("SECURE"))]
finally:
    eng.live_prices = real_prices
check("Twisted's H100 at $3.49/h is not in the plan, nor the L40S at $1.09/h",
      planned and "NVIDIA H100 80GB HBM3" not in planned and "NVIDIA L40S" not in planned, str(planned))

# The one that cost a pod to learn. torch 2.3.1+cu121 has no sm_120/sm_100
# kernels, so a Blackwell card rents, boots, loads the model, answers /health
# with "ok" and then fails EVERY chunk with "no kernel image is available for
# execution on the device". Measured on an RTX PRO 4500 Blackwell, 2026-09-22.
# The :4.x images are cu128; a Blackwell card is rented only once measured.
BLACKWELL = ("RTX PRO 4500", "RTX PRO 6000 Blackwell", "RTX 5090", "GeForce RTX 50",
             "B200", "B300", "MIG")
check("no Blackwell card is rented before one is measured",
      not [n for n in eng.cost_plan.SPEED if any(b in n for b in BLACKWELL)])

print("\n🎛️  RunPod's price list, as RunPod sends it")


class _PriceAnswer:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def prices_from(body, raises=None):
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            if raises:
                raise raises
            return _PriceAnswer(body)
    real = eng.httpx.AsyncClient
    eng.httpx.AsyncClient = _C
    try:
        return asyncio.run(eng.live_prices())
    finally:
        eng.httpx.AsyncClient = real


quoted = prices_from({"data": {"gpuTypes": [
    {"id": "NVIDIA RTX A5000", "securePrice": 0.27, "communityPrice": 0.16,
     "secure": {"uninterruptablePrice": 0.27, "stockStatus": "Low"},
     "community": {"uninterruptablePrice": None, "stockStatus": None}},
    {"id": "NVIDIA L4", "securePrice": 0.49, "communityPrice": 0.44,
     "secure": {"uninterruptablePrice": None, "stockStatus": "High"},
     "community": {"uninterruptablePrice": 0.44, "stockStatus": "None"}}]}})
check("a card in stock is priced at RunPod's quote", quoted[("NVIDIA RTX A5000", "SECURE")] == 0.27, str(quoted))
check("a card out of stock is priced 0, so it is never tried",
      quoted[("NVIDIA RTX A5000", "COMMUNITY")] == 0 and quoted[("NVIDIA L4", "COMMUNITY")] == 0)
check("in stock with no quote falls back to the listed price", quoted[("NVIDIA L4", "SECURE")] == 0.49)
check("RunPod not answering gives no prices, not a crash",
      prices_from(None, raises=eng.httpx.ConnectError("down")) == {})
check("an answer with errors and no data gives no prices", prices_from({"errors": [{"message": "x"}]}) == {})

said = io.StringIO()
real_prices = eng.live_prices


async def too_dear():
    return {(g, "SECURE"): 50.0 for g in eng.cost_plan.SPEED}

eng.live_prices = too_dear
try:
    with contextlib.redirect_stdout(said):
        got = asyncio.run(eng._sweep_for_gpu("SECURE"))
        n = asyncio.run(eng.planned_pod_count([{"text": "x" * 1000}]))
finally:
    eng.live_prices = real_prices
check("no card under the budget: nothing is tried, and it says why",
      got is None and "No card in stock on SECURE" in said.getvalue(), said.getvalue().strip()[:120])
check("and the pool size falls back to POD_COUNT, for when stock returns", n == eng.POD_COUNT)

print("\n🎛️  How many pods a book gets")

# 2026-10-22: Winning (6.9 h of audio, an A5000 at $0.27) ran on ONE pod because
# POD_COUNT defaulted to 1: 48 minutes of narrating, 75 in all, for $0.29 of a
# budget that paid for two. The default is now "up to 4", cut by the dollar rule.
import re as _re
check("POD_COUNT defaults to 4, not 1 (the budget cuts it)",
      _re.search(r'POD_COUNT = int\(os\.getenv\("POD_COUNT", "4"\)\)', open(eng.__file__).read()))
eng.cost_plan.set_other_spend(0.05, 7.8)
spd = eng.cost_plan.SPEED["NVIDIA RTX A5000"]
winning = eng.cost_plan.pods_for(7.8, 0.27, spd, 4)
check("Winning on an A5000 gets four pods: 26 minutes, not 65, inside its budget and allowance",
      winning == 4, f"({winning})")
check("...and they cost under what the book is allowed",
      eng.cost_plan.book_cost(0.27, spd, 7.8, 4) + 0.05 <= eng.cost_plan.allowed_dollars(7.8) + 0.05)
check("the allowance is for boots only: without it the same book gets fewer",
      (os.environ.__setitem__("VOXIE_SPEED_ALLOWANCE", "0"), eng.cost_plan.pods_for(7.8, 0.27, spd, 4))[1] < 4)
os.environ.pop("VOXIE_SPEED_ALLOWANCE", None)
eng.cost_plan.set_other_spend(0.0, 0.0)
check("a 16-hour book gets all four",
      eng.cost_plan.pods_for(16.0, 0.27, spd, 4) == 4)


async def a5000_only():
    return {("NVIDIA RTX A5000", "SECURE"): 0.27}

sixteen_hours = [{"text": "x" * int(16 * 3600 * eng.cost_plan.CHARS_PER_SECOND / 100)}] * 100
real = (eng.live_prices, eng.POD_COUNT, eng.CLOUD_ORDER)
eng.live_prices, eng.CLOUD_ORDER = a5000_only, ["SECURE"]
try:
    said = io.StringIO()
    for wanted in (1, 3, 12):
        eng.POD_COUNT = wanted
        with contextlib.redirect_stdout(said):
            n = asyncio.run(eng.planned_pod_count(sixteen_hours))
        cost = eng.cost_plan.book_cost(0.27, eng.cost_plan.SPEED["NVIDIA RTX A5000"], 16.0, n)
        check(f"POD_COUNT={wanted}: {n} pod(s), ${cost:.2f} for 16 hours",
              1 <= n <= wanted and cost <= eng.cost_plan.PLANNING_MARGIN * 1.0)
    check("the estimate is said before anything bills", "About 16.0 h of audio" in said.getvalue()
          and "$" in said.getvalue(), said.getvalue()[:120])
    check("twelve pods asked for are cut to what the budget allows",
          "keep(s) the budget" in said.getvalue())

    # Twisted, 2026-10-08: the director and the filters had spent $0.25 on
    # OpenRouter before any GPU; scaled to 16 hours that is about $0.29.
    eng.POD_COUNT, eng.RUN_SPEND["openrouter"] = 3, 0.29
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        n = asyncio.run(eng.planned_pod_count(sixteen_hours))
    whole = eng.cost_plan.book_cost(0.27, eng.cost_plan.SPEED["NVIDIA RTX A5000"], 16.0, n) + 0.29
    check(f"with $0.29 already spent on OpenRouter: {n} pod(s), ${whole:.2f} in all",
          whole <= eng.cost_plan.PLANNING_MARGIN * 1.0, said.getvalue().strip()[:200])
    check("and the estimate says the OpenRouter part and the whole",
          "already spent on OpenRouter" in said.getvalue() and "in all" in said.getvalue())
finally:
    eng.live_prices, eng.POD_COUNT, eng.CLOUD_ORDER = real
    eng.RUN_SPEND["openrouter"] = 0.0
    eng.cost_plan.set_other_spend(0.0, 0.0)

print("\n🎛️  A pod billed over its ceiling goes at once")
over = []


class _Pricey:
    status_code = 200

    @staticmethod
    def json():
        return {"data": {"podFindAndDeployOnDemand": {"id": f"pod{len(over)}", "costPerHr": 9.99}}}


class _Client3:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, timeout=None):
        over.append(json["query"])
        return _Pricey()


released = []


async def _release(pod_id):
    released.append(pod_id)
    return True

import tempfile  # noqa: E402
pod_pool.REGISTRY = os.path.join(tempfile.mkdtemp(), "pods.json")   # never the real registry
real = (eng.httpx.AsyncClient, eng.live_prices, eng.release_pod)
eng.httpx.AsyncClient, eng.live_prices, eng.release_pod = _Client3, no_quotes, _release
try:
    got = asyncio.run(eng._sweep_for_gpu("SECURE"))
finally:
    eng.httpx.AsyncClient, eng.live_prices, eng.release_pod = real
check("a pod that comes at $9.99/h is terminated, not used",
      got is None and len(released) == len(over) > 0, f"{len(released)} released of {len(over)}")

print("\n🎛️  A deploy RunPod refuses for good stops at once (2026-10-07)")
# "Template not found" arrives like "no capacity", inside an HTTP 200. Treated
# as capacity, a typo in RUNPOD_TEMPLATE_ID sat out the 45-minute wait.
asked = []


class _NoTemplate:
    status_code = 200

    @staticmethod
    def json():
        return {"errors": [{"message": "Template not found"}]}


class _Client2:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, timeout=None):
        asked.append(json)
        return _NoTemplate()


real_client, real_prices = eng.httpx.AsyncClient, eng.live_prices
eng.httpx.AsyncClient, eng.live_prices = _Client2, no_quotes
try:
    asyncio.run(eng._sweep_for_gpu("COMMUNITY"))
    raised = None
except eng.DeployRefusedError as e:
    raised = e
finally:
    eng.httpx.AsyncClient, eng.live_prices = real_client, real_prices
check("a missing template stops the sweep on the first card",
      isinstance(raised, eng.TemplateNotFoundError) and len(asked) == 1, f"{len(asked)} requests")
check("and says which setting to fix, and that nothing is billed",
      raised is not None and "RUNPOD_TEMPLATE_ID" in str(raised) and "Nothing was billed" in str(raised))
check("out of funds is the same kind of refusal", issubclass(eng.OutOfFundsError, eng.DeployRefusedError))

real_template = eng.RUNPOD_TEMPLATE_ID
try:
    for value, ok in (("y1s4i5a5qa#x1y2z3w4v5", False), ("y1s4i5a5qa", True)):
        eng.RUNPOD_TEMPLATE_ID = value
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                eng.check_prerequisites("/tmp", own_server=False)
            except SystemExit:
                pass
        says = "not a RunPod template id" in buf.getvalue()
        check(f"a template id like {value!r} is {'accepted' if ok else 'refused before anything runs'}",
              says != ok)
finally:
    eng.RUNPOD_TEMPLATE_ID = real_template

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
