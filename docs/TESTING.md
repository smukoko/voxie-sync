# How Voxie is tested

Voxie is written with a lot of help from AI coding assistants, and anyone may
contribute with theirs. So the rule is not "trust the author", human or AI. The
rule is that **code is only merged when an automated harness proves it works**,
and the harness is built so that a test which cannot fail does not count.

The idea is Clever Cloud's (Quentin Adam): treat an AI assistant as a very fast
junior developer, and spend what it saves on a testing harness that checks
every change. Four layers, each a required check on every pull request.

| Layer | What it proves | Where |
| --- | --- | --- |
| 1. Unit | Each piece of logic gives the right answer, and every new line is tested by a test that fails without it | `tools/test_*.py`, `tools/run_all_tests.sh` |
| 2. Integration | The real parts work together: engine, watcher, gateway, Audiobookshelf, KOReader, with fake paid services | `tools/integration/` |
| 3. Fault injection | Nothing is lost or paid twice when pods die, the network drops, the server is down or a run is killed | `tools/chaos/` |
| 4. Security | No known vulnerability, no secret, no endpoint open to the wrong person; an AI auditor reads every change | `.github/workflows/security.yml` |

Status (2026-10-08): layer 1 is in place; layers 2 and 3 run nightly and on a
labelled pull request; layer 4's scans and the gateway attack test run on every
pull request.

## 1. Unit tests

Every module has a `tools/test_<thing>.py`; `tools/run_all_tests.sh` runs them
all, and the Lua plugin's tests run under LuaJIT.

Three gates make a unit test count (all three run on every pull request):

- **Coverage may only go up.** Every test runs under `coverage`; the total
  may not drop below the recorded baseline (`tools/coverage_baseline.json`),
  and the lines a pull request adds or changes must be covered.
- **A new test must fail on the old code.** For a pull request that changes
  behaviour, CI runs the pull request's tests against the code from before
  it. If they all pass there too, they test nothing, and the check fails.
  A pure refactor says so with the `no-behaviour-change` label.
- **Mutation testing.** A tool plants small bugs on purpose (a `<` becomes
  `<=`, a condition is dropped) and runs the tests; a bug they do not notice
  is a gap. On a pull request, every bug that can be planted in the lines it
  changed is tried, and at least 70% must be caught. Nightly, a sample of
  every logic module is scored, as a trend to read.

## 2. Integration tests

The real programs, wired together, with nothing faked but the reader and the
paid services.

**The free route end to end** (`tools/integration/free_route.py`, about 15
minutes, needs Docker): a real Audiobookshelf from its pinned image, the real
gateway, and the real watcher and engine narrating with Kokoro on the CPU and
aligning with MFA, all in a scratch folder with its own home, so a real
library is never touched. The book is generated (original prose, an EPUB 3
table of contents), so no copyrighted text is involved. It checks, each as a
PASS or FAIL line:

1. a dropped English book is narrated, uploaded and archived;
2. Audiobookshelf shows it once, with the manifest's duration, a chapter
   marker where every file starts, and the book's own chapter titles;
3. the gateway serves the device's map, intact;
4. sync both ways through the real APIs: a Kobo page moves the audiobook to
   that paragraph, listening moves the Kobo back, never across a chapter start;
5. the same book narrated again at another speed (a regeneration) leaves one
   track per file, the new duration and the new markers;
6. an item whose stored duration went stale (Twisted, 2026-10-07) is repaired
   by the gateway against the real Audiobookshelf;
7. a book dropped without audio, then with it, stays one book;
8. a book that is not English reaches the Kobo as an ebook only.

It runs nightly, by hand (`workflow_dispatch`), and on a pull request
labelled `integration` (`.github/workflows/integration.yml`). Run it before a
pull request that touches the engine, the watcher or the gateway:

    python tools/integration/free_route.py

**A real book against a real server** (`tools/verify_book.py "Title"`,
read-only): the after-run claims for a book already in a library, checked
against its files, the real Audiobookshelf and the real gateway.

Still to build: the GPU route against a fake RunPod and a fake CosyVoice
server, and the real KOReader (`tools/kosim`) syncing both ways, online and
offline.

## 3. Fault injection

Voxie is a small distributed system: a computer, rented GPUs, a server, a
phone and an e-reader. What goes wrong between them is what has cost real
money so far.

**The GPU route when things go wrong** (`tools/chaos/run.py`, about 15
minutes, needs Docker and espeak-ng). The real engine, unchanged, against a
fake RunPod and fake narration pods on this machine (`tools/chaos/fakes.py`):
RunPod's GraphQL and REST answers, and pods that check the token, speak the
text with espeak-ng (as 32-bit float WAV, like CosyVoice) and fail on cue.
The engine reaches them through `RUNPOD_GRAPHQL_URL` and `RUNPOD_REST_URL`,
and aligns with the real MFA, so every scenario ends in a real manifest:

| Scenario | What must hold |
| --- | --- |
| healthy | two pods narrate the book; every pod is terminated after |
| never-boots | a pod that never serves is terminated and replaced |
| dies-mid-book | a pod lost mid-book: its chunks finish on the other one |
| only-pod-dies | the only pod lost mid-book: terminated within a minute, replaced, the book made |
| flaky-server | 503 on every other chunk: retried, nothing quarantined |
| runpod-errors | RunPod's errors inside HTTP 200 are waited out |
| over-ceiling | a pod billed over its ceiling is returned within seconds |
| out-of-funds | a spent account stops the run at once and says to top up |
| open-door | a pod that ignores the token is refused before any chunk |
| kill-9 | the engine killed mid-book: the watcher's sweep ends every pod, and the next run pays only for what was not narrated |
| nothing-affordable | no card under the budget: nothing is rented, and it says why |

    python tools/chaos/run.py                  # all of them
    python tools/chaos/run.py kill-9 healthy   # some

It runs nightly, by hand, and on a pull request labelled `chaos`
(`.github/workflows/chaos.yml`). Writing it found that a spent account's
"top up" message was cut to 60 characters and the run ended "No pod served",
and that a book on one pod was lost when that pod died: the engine asked the
dead pod twelve times per chunk and quarantined them all. A lost pod is now
terminated and replaced.

Still to build: network faults between the engine and the fakes (a proxy
that drops and resets), the server down during an upload and
Audiobookshelf down during a sync with the real programs (both are unit
tested today), and a full disk.

## 4. Security

On every pull request:

- static analysis (`bandit`, `semgrep`), known vulnerabilities in dependencies
  (`pip-audit`), secrets (`gitleaks`), the export's own leak scan;
- the gateway attacked in a container: every endpoint without credentials,
  with another account's credentials, with path traversal and zip tricks in an
  upload, with a server address pointing inside the network (OWASP ZAP plus
  Voxie's own checks);
- an AI security review of the diff.

## The rules for contributors, and their AI tools

- Run `tools/run_all_tests.sh` before you open a pull request.
- New behaviour comes with a test that fails without it.
- Never weaken a test to make it pass, and never skip a gate. If a gate is
  wrong, fix the gate in its own pull request and say why.
