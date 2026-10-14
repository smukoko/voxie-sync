#!/bin/bash
# Every offline suite: no GPU, no network. Exits non-zero if any fails.
# test_fat_check needs Docker and skips loudly without it.
cd "$(dirname "$0")/.." || exit 1
failed=()
for t in tools/test_*.py; do
    if out=$(python -u "$t" 2>&1 </dev/null); then
        echo "ok    $t"
    else
        echo "FAIL  $t"; echo "$out" | tail -15; failed+=("$t")
    fi
done
for t in tools/test_koplugin.lua tools/test_landing.lua tools/test_wifi_patch.lua; do
    if ! command -v luajit >/dev/null; then echo "skip  $t (no luajit)"
    elif out=$(luajit "$t" 2>&1); then echo "ok    $t"
    else echo "FAIL  $t"; echo "$out" | grep -v "^✅\|^PASS" | tail -15; failed+=("$t"); fi
done
echo "${#failed[@]} failed"
[ ${#failed[@]} -eq 0 ]
