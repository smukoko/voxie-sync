"""The two paid services Voxie recommends, the one referral link, and where to donate.

Decided by the user on 2026-10-04: the recommended setup is a Hetzner server
and RunPod narration; on 2026-10-07 JEV through OpenRouter joined it, as the
narration director. RunPod's link carries the project's referral code, said
openly wherever it appears; Hetzner no longer runs a referral program, so its
link is a plain one. Every paid route keeps its free one
(tools/test_free_routes.py). README.md and DEPLOY.md repeat these links;
tools/test_one_command_setup.py checks they agree, and tools/export_public.sh
refuses to export while any still holds the placeholder.

Donations go through Ko-fi (user, 2026-10-05, instead of GitHub Sponsors).
.github/FUNDING.yml, funding.json and README.md repeat the link; the same
test checks they agree.
"""

PLACEHOLDER = "REFERRAL_CODE_HERE"

HETZNER_URL = "https://www.hetzner.com/cloud"
RUNPOD_URL = "https://runpod.io?ref=x88lhsfk"
# The narration director (JEV) and the optional junk filter are reached through
# OpenRouter (user, 2026-10-07: part of the recommended setup). No referral.
OPENROUTER_URL = "https://openrouter.ai"

DISCLOSURE = ("The RunPod link is a referral link: Voxie gets a small credit when you "
              "sign up through it, and you pay nothing extra.")

DONATE_URL = "https://ko-fi.com/voxiesync"
