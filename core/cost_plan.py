"""What a book may cost on RunPod, and which cards can narrate it for that.

The promise (user, 2026-10-08): a 16-hour novel narrated for under $1. Twisted
broke it at $1.70 for 13.5 hours: three secure pods, one of them an H100 at
$3.49 an hour, because the engine rented whatever was in stock down a list
ordered by memory and price, with no ceiling.

A book costs, per pod, its price times the hours it is billed:

    cost = price x (audio_hours / (speed x pods) + boot_hours) x pods
         = price x (audio_hours / speed + pods x boot_hours)

`speed` is the realtime multiple one pod sustains on the real workload, and
it is measured, never assumed (tools/gpu_bench.py rents the card, renders a
real book's directed chunks and checks them with Whisper). So:

  - only cards with a measured speed are rented at all;
  - a card is allowed only while one pod of it narrates 16 hours, boot
    included, within the budget, at the price RunPod quotes right now;
  - they are tried cheapest per audio hour first, so price / speed;
  - more pods only while the extra boots still fit the budget.

Pure functions: no network here. The engine fetches prices and deploys.
"""
import os

BOOK_HOURS = 16.0

# Dollars for a 16-hour book. VOXIE_BUDGET_16H moves it (a user who wants a
# faster card and accepts the bill); the default is the promise.
DEFAULT_BUDGET_16H = 1.0

# Deploy to the first answer, measured 2026-10-07 on the public :4.1 template
# (secure cloud): 5.8 to 12.8 minutes. A pod bills from the deploy.
BOOT_HOURS = 13 / 60

# Plan against 90% of the budget. The model leaves out what a real run adds:
# a boot that fails and is retried, workers idle at the tail, the narration
# director's calls to OpenRouter. The margin keeps those inside the promise.
PLANNING_MARGIN = 0.9

# Characters of narrated text per second of audio, from the same benchmark
# (46,249 characters made 4,018 s): turns a book's text into hours before
# anything is rented. 16 hours is about 660,000 characters.
CHARS_PER_SECOND = 11.5

# Realtime multiple per pod on the live server (vLLM CosyVoice 3, 8 callers,
# the narration director on), by RunPod gpuTypeId. Measured by
# tools/gpu_bench.py on 2026-10-07; every result is in
# docs/measurements/gpu_bench.json. A card missing here is never rented: an
# unmeasured card is a guess at the bill.
#
#     card        $/h secure   speed   16 h on one pod
#     RTX A5000   0.27         9.1x    $0.51
#     RTX 3090    0.50         9.7x    $0.94
#     A40         0.49         6.9x    $1.25
#     L4          0.49         6.6x    $1.25
#     RTX 4090    0.89        21.3x    $0.74, in 50 min (2026-10-10, :4.2)
#     RTX A6000   0.59         9.7x    $1.06 (2026-10-10, :4.2)
#
# On 2026-10-07 the 4090 and the A6000 served /health and never finished the
# 160 chunks; re-run on :4.2 on 2026-10-10 with every request timed, both
# finished with no failed request (4090 median 5.7 s a chunk, A6000 15.2 s).
# A speed does not rent a card: the ceiling does. With the director's
# OpenRouter share, the 4090 keeps the promise only under about $0.63/h and
# the A6000 under $0.33/h, so at these prices only a raised VOXIE_BUDGET_16H
# rents them (the 4090 then narrates 16 hours in under an hour). Whisper heard
# every word on the 2026-10-07 cards and 92% on these two, the same takes of
# a different book (Twisted) on both. The old 3.6x figures were the pre-vLLM
# server. Twisted's H100 was never measured, and at $3.49 it would need about
# 390x to keep the promise.
SPEED = {
    "NVIDIA RTX A5000": 9.1,
    "NVIDIA GeForce RTX 3090": 9.7,
    "NVIDIA A40": 6.9,
    "NVIDIA L4": 6.6,
    "NVIDIA GeForce RTX 4090": 21.3,
    "NVIDIA RTX A6000": 9.7,
}


# What this book has already spent that is not GPU (the director's and the
# filters' OpenRouter calls), and the hours of audio it is for. Set by the
# engine once the plan is made, before anything is rented.
_OTHER = {"usd": 0.0, "hours": 0.0}


def set_other_spend(usd, hours):
    _OTHER.update(usd=max(0.0, float(usd or 0.0)), hours=max(0.0, float(hours or 0.0)))


def other_per_16h():
    """The non-GPU spend so far, scaled to a 16-hour book."""
    return _OTHER["usd"] * BOOK_HOURS / _OTHER["hours"] if _OTHER["hours"] else 0.0


def gpu_budget_16h(budget=None):
    """What the GPU may cost for 16 hours: the margin of the budget, less what
    the book has spent on everything else."""
    budget = budget_16h() if budget is None else budget
    return PLANNING_MARGIN * budget - other_per_16h()


def budget_16h():
    try:
        return float(os.getenv("VOXIE_BUDGET_16H", DEFAULT_BUDGET_16H))
    except ValueError:
        return DEFAULT_BUDGET_16H


def audio_hours(characters):
    """How long the narration of this much text will be, in hours."""
    return characters / CHARS_PER_SECOND / 3600


def book_cost(price, speed, hours=BOOK_HOURS, pods=1, boot_hours=BOOT_HOURS):
    """Dollars for `hours` of audio on `pods` pods of one card."""
    return price * (hours / speed + pods * boot_hours)


def ceiling(speed, budget=None, boot_hours=BOOT_HOURS):
    """The most an hour of this card may cost while one pod keeps the promise."""
    return gpu_budget_16h(budget) / (BOOK_HOURS / speed + boot_hours)


def candidates(prices, budget=None, speed=None):
    """[(gpu, cloud, price, ceiling)] worth deploying, cheapest per audio hour first.

    `prices` is {(gpu, cloud): $/h} as RunPod quotes it now (None or 0 when it
    has no stock). Cards without a measured speed, and cards whose price is
    over their ceiling, are left out.
    """
    speed = SPEED if speed is None else speed
    rows = []
    for (gpu, cloud), price in prices.items():
        s = speed.get(gpu)
        if not s or not price:
            continue
        top = ceiling(s, budget)
        if price <= top:
            rows.append((price / s, gpu, cloud, price, round(top, 3)))
    return [(gpu, cloud, price, top) for _, gpu, cloud, price, top in sorted(rows)]


# A boot costs the same for a short book as for a long one, so a share of the
# budget proportional to length leaves a 7-hour book too little to pay for the
# pods that would make it 25 minutes faster (user, 2026-10-22: more pods, yes).
# Dollars a book may spend beyond its share, on boots only; it fades to nothing
# at 16 hours, so the promise ($1.00 for a 16-hour book, everything counted) is
# exactly what it was. VOXIE_SPEED_ALLOWANCE=0 turns it off.
def speed_allowance():
    try:
        return max(0.0, float(os.getenv("VOXIE_SPEED_ALLOWANCE", "0.20")))
    except ValueError:
        return 0.20


def allowance_for(hours):
    """The speed allowance a book of `hours` gets: all of it for a short one, none at 16 hours."""
    return speed_allowance() * max(0.0, 1.0 - hours / BOOK_HOURS)


def allowed_dollars(hours, budget=None):
    """What the GPU of a book of `hours` may cost: its share of the budget, and the allowance."""
    return gpu_budget_16h(budget) * max(hours, 1.0) / BOOK_HOURS + allowance_for(hours)


def pods_for(hours, price, speed, wanted, budget=None, boot_hours=BOOT_HOURS):
    """How many pods of one card a book of `hours` may use: at most `wanted`,
    and only as many as keep it within what `allowed_dollars` says."""
    allowed = allowed_dollars(hours, budget)
    n = max(1, wanted)
    while n > 1 and book_cost(price, speed, hours, n, boot_hours) > allowed:
        n -= 1
    return n
