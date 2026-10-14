"""The cost planner keeps the promise: a 16-hour book for under $1 (core/cost_plan.py).

Twisted (2026-10-07) cost $1.70 for 13.5 hours because nothing capped the
price: an H100 at $3.49 an hour was rented because it was in stock.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cost_plan  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


SPEED = {"cheap slow": 10.0, "fast": 40.0, "h100": 45.0}
PRICES = {("cheap slow", "SECURE"): 0.27, ("fast", "SECURE"): 0.74, ("fast", "COMMUNITY"): 0.34,
          ("h100", "SECURE"): 3.49, ("unmeasured", "COMMUNITY"): 0.10, ("cheap slow", "COMMUNITY"): None}

plan = cost_plan.candidates(PRICES, budget=1.0, speed=SPEED)
names = [(g, c) for g, c, _, _ in plan]
check("an H100 at $3.49 is never rented for a $1 book", ("h100", "SECURE") not in names, str(names))
check("an unmeasured card is never rented, however cheap", all(g != "unmeasured" for g, _ in names))
check("a card out of stock is skipped", ("cheap slow", "COMMUNITY") not in names)
check("cheapest per audio hour first, not cheapest per hour",
      names == [("fast", "COMMUNITY"), ("fast", "SECURE"), ("cheap slow", "SECURE")], str(names))
check("every card allowed keeps a 16-hour book within $1, boot included",
      all(cost_plan.book_cost(p, SPEED[g]) <= 1.0 for g, _, p, _ in plan))
check("and a card just over its ceiling is refused",
      not cost_plan.candidates({("fast", "SECURE"): cost_plan.ceiling(40.0, 1.0) + 0.01}, budget=1.0, speed=SPEED))

slow_price = 0.27
check("a slow card that would cost over $1 is refused even at a low price",
      not cost_plan.candidates({("crawl", "SECURE"): slow_price}, budget=1.0, speed={"crawl": 4.0}),
      f"(16 h at 4x on ${slow_price}/h = ${cost_plan.book_cost(slow_price, 4.0):.2f})")

check("a bigger budget allows a dearer card",
      cost_plan.candidates({("h100", "SECURE"): 3.49}, budget=3.0, speed=SPEED) != [])
os.environ["VOXIE_BUDGET_16H"] = "2"
check("VOXIE_BUDGET_16H sets that budget", cost_plan.budget_16h() == 2.0)
os.environ["VOXIE_BUDGET_16H"] = "two dollars"
check("a budget that is not a number falls back to the promise", cost_plan.budget_16h() == 1.0)
del os.environ["VOXIE_BUDGET_16H"]

n = cost_plan.pods_for(16.0, 0.34, 40.0, wanted=3, budget=1.0)
check("pods are capped so their boots still fit", 1 <= n <= 3
      and cost_plan.book_cost(0.34, 40.0, 16.0, n) <= 1.0, f"({n} pods)")
check("a card at its ceiling gets one pod, whatever was asked",
      cost_plan.pods_for(16.0, cost_plan.ceiling(40.0, 1.0), 40.0, wanted=4, budget=1.0) == 1)
check("one pod is always allowed", cost_plan.pods_for(16.0, 9.0, 1.0, wanted=1, budget=1.0) == 1)
check("a short book's pods fit its share of the budget",
      cost_plan.book_cost(0.34, 40.0, 2.0, cost_plan.pods_for(2.0, 0.34, 40.0, wanted=3, budget=1.0)) <= 2.0 / 16)

hours = cost_plan.audio_hours(16 * 3600 * cost_plan.CHARS_PER_SECOND)
check("text turns into hours of audio", abs(hours - 16.0) < 1e-9, f"({hours})")

check("the measured table is not empty, and every card in it can keep the promise somewhere",
      cost_plan.SPEED and all(cost_plan.ceiling(s, 1.0) > 0.1 for s in cost_plan.SPEED.values()))
live = {("NVIDIA RTX A5000", "SECURE"): 0.27, ("NVIDIA GeForce RTX 3090", "SECURE"): 0.50,
        ("NVIDIA A40", "SECURE"): 0.49, ("NVIDIA L4", "SECURE"): 0.49}
picked = [g for g, _, _, _ in cost_plan.candidates(live, budget=1.0)]
check("at 2026-10-07's secure prices the A5000 is chosen, and the cards that miss the promise are not",
      picked and picked[0] == "NVIDIA RTX A5000" and "NVIDIA A40" not in picked and "NVIDIA L4" not in picked,
      str(picked))

# --- The whole bill: what the director and the filters spent counts too ------
# Twisted, 2026-10-08: 13.8 h of audio, $0.25 on OpenRouter before any GPU.
a5000 = cost_plan.SPEED["NVIDIA RTX A5000"]
cost_plan.set_other_spend(0.0, 0.0)
alone = cost_plan.pods_for(13.8, 0.27, a5000, wanted=3, budget=1.0)
cost_plan.set_other_spend(0.25, 13.8)
pods = cost_plan.pods_for(13.8, 0.27, a5000, wanted=3, budget=1.0)
whole = cost_plan.book_cost(0.27, a5000, 13.8, pods) + 0.25
check("with OpenRouter's $0.25 counted, Twisted's whole bill stays inside the margin",
      whole <= cost_plan.PLANNING_MARGIN * 1.0 * 13.8 / 16, f"({pods} pod(s), ${whole:.2f} for 13.8 h)")
check("and fewer pods are allowed than when the GPU was counted alone", pods < alone, f"({alone} -> {pods})")
check("the ceilings come down by what was spent",
      cost_plan.ceiling(a5000, 1.0) < cost_plan.PLANNING_MARGIN / (16 / a5000 + cost_plan.BOOT_HOURS))
cost_plan.set_other_spend(5.0, 2.0)
check("a book that spent the whole budget elsewhere rents nothing",
      cost_plan.candidates({("NVIDIA RTX A5000", "SECURE"): 0.27}, budget=1.0) == [])
cost_plan.set_other_spend(1.0, 0.0)
check("spend with no hours yet does not divide by zero", cost_plan.other_per_16h() == 0.0)
cost_plan.set_other_spend(0.0, 0.0)

# --- SPEED is the benchmark's, card for card --------------------------------------
# A speed is what rents a card, so it must be measured: the latest run of each
# card that finished the workload (tools/gpu_bench.py, every result kept in
# docs/measurements/gpu_bench.json), and nothing else.
import json  # noqa: E402

runs = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "docs", "measurements", "gpu_bench.json")))
finished = {}
for r in runs:
    if r.get("realtime") and not r.get("error") and not r.get("plain"):
        finished[r["gpu"]] = round(r["realtime"], 1)
check("every card with a speed has a finished benchmark, and the speed is its measured one",
      all(finished.get(g) == s for g, s in cost_plan.SPEED.items()),
      f"({ {g: (s, finished.get(g)) for g, s in cost_plan.SPEED.items() if finished.get(g) != s} })")
check("every card that finished the benchmark has its speed", set(finished) <= set(cost_plan.SPEED),
      f"(missing: {sorted(set(finished) - set(cost_plan.SPEED))})")

# --- The 4090 (21.3x, 2026-10-10): fast, rented only when the budget allows ---------
cost_plan.set_other_spend(0.25, 13.8)       # Twisted's director and filters
os.environ.pop("VOXIE_BUDGET_16H", None)
quote = {("NVIDIA GeForce RTX 4090", "SECURE"): 0.89, ("NVIDIA RTX A5000", "SECURE"): 0.27}
check("at $0.89 the default budget rents the A5000, not the 4090",
      [g for g, *_ in cost_plan.candidates(quote)] == ["NVIDIA RTX A5000"], str(cost_plan.candidates(quote)))
check("a budget of $1.50 per 16 hours rents the 4090 too: the same book in under half the time",
      "NVIDIA GeForce RTX 4090" in [g for g, *_ in cost_plan.candidates(quote, budget=1.5)],
      str(cost_plan.candidates(quote, budget=1.5)))
cost_plan.set_other_spend(0.0, 0.0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
