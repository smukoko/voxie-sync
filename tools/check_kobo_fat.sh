#!/bin/bash
# Prove that what setup-kobo writes survives the Kobo's own filesystem check.
#
# macOS's FAT driver can hand the same 8.3 short name to two different long
# filenames. The Linux fsck.vfat the Kobo runs at boot "repairs" that by
# renaming one of the pair to FSCK0000.000, and KOReader dies on launch.
# Nothing on the Mac reveals it, which is why it needs a rig.
#
# The logic lives in libra_cli/fat_check.py so setup_kobo can gate on it; this
# is the command-line entry point, kept for its original usage:
#
#   ./tools/check_kobo_fat.sh path/to/koreader-kobo-vX.zip
#
# and for the full payload, with the device's existing names seeded in:
#
#   python -m libra_cli.fat_check --zip koreader.zip:.adds \
#       --tree ~/.librasync_cache/dicts/en-en:.adds/koreader/data/dict/en-en \
#       --tree fonts/:fonts --seed /Volumes/KOBOeReader
#
# Exit 0 = clean. Re-run when bumping the KOReader release.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -eq 1 ] && [ "${1#-}" = "$1" ]; then
    exec python3 -m libra_cli.fat_check --zip "$1:.adds"
fi
exec python3 -m libra_cli.fat_check "$@"
