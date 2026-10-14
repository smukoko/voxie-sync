# Security

## Reporting a vulnerability

Please report it privately, never in a public issue: open the repository's
**Security** tab and choose **Report a vulnerability** (GitHub's private
vulnerability reporting). Only the maintainers see it.

Say what an attacker can do, and how to reproduce it. You will get an answer
within a week. Once a fix is released you will be credited in the changelog
unless you would rather not be.

## What is in scope

Voxie runs on machines its users own, so the parts that face a network matter
most:

- **The gateway** (`librerian_gateway_v3.py`): kosync and OPDS
  authentication, Sync Pack upload (`UPLOAD_TOKEN`), each account's
  Audiobookshelf token (stored encrypted with `LIBRA_SECRET_KEY`), and the
  rule that an account only reaches its own books and positions.
- **The TTS servers** (`server/custom_api.py`, `server/cpu_tts.py`): the
  `TTS_API_TOKEN` check.
- **The KOReader plugin** (`librarian_sync.koplugin/`) and the Kobo
  installer (`libra_cli/`): anything that writes to the device.
- **Secrets**: anything that could print, log or send a key or token.

Out of scope: DRM. Voxie does not remove or work around it, and reports
asking it to will be closed.

## Supported versions

Only the latest release gets security fixes.
