#!/bin/bash
# Build a throwaway world for driving the real KOReader against the gateway:
#   the official KOReader Linux arm64 build, our plugin pointed at a local gateway
#   on :3099, a copy of library/database.db with one book paired to its real
#   KOReader hash, and a fake Audiobookshelf on :13399 whose position you control
#   through $W/abs_state.json. Nothing touches the live gateway, ABS or a device.
#
#   tools/kosim/setup.sh "Redeeming Love" LBR-dd157de7f7a1     # then see README.md
set -e
TITLE="$1"; LIB="$2"; R="$(cd "$(dirname "$0")/../.." && pwd)"; W="${KOSIM_DIR:-/tmp/kosim}"
VER="${KOREADER_VERSION:-v2026.07.1}"
mkdir -p "$W"/{gw/library,home/.config/koreader/settings,books,out}
[ -d "$W/lib/koreader" ] || { curl -sL -o "$W/ko.tar.xz" "https://github.com/koreader/koreader/releases/download/$VER/koreader-linux-arm64-$VER.tar.xz"; tar -xJf "$W/ko.tar.xz" -C "$W"; }
cp "$R/tools/kosim/"{Dockerfile,run.sh,mock_abs.py} "$W/"
docker build --platform linux/arm64 -q -t koemu "$W" >/dev/null
mkdir -p "$W/lib/koreader/plugins/librarian_sync.koplugin"
for f in "$R"/librarian_sync.koplugin/*.lua; do
  sed "s#__GATEWAY_URL__#http://host.docker.internal:3099#" "$f" > "$W/lib/koreader/plugins/librarian_sync.koplugin/$(basename "$f")"
done
# One folder per book (core/library.py); library/ebooks is the legacy layout.
KEPUB="$R/library/audiobooks/$TITLE/$TITLE.kepub.epub"
[ -f "$KEPUB" ] || KEPUB="$R/library/ebooks/$TITLE/$TITLE.kepub.epub"
cp "$KEPUB" "$W/books/$TITLE.epub"
cp "$R/library/database.db" "$W/gw/library/database.db"
python3 - "$W" "$LIB" "$KEPUB" <<'PY'
import hashlib, sqlite3, sys
w, lib, kepub = sys.argv[1:]
h = hashlib.md5()
with open(kepub, "rb") as f:          # KOReader's util.partialMD5; LuaJIT lshift(1024,-2) == 0
    for i in range(-1, 11):
        f.seek(0 if i < 0 else 1024 << (2 * i)); s = f.read(1024)
        if not s: break
        h.update(s)
c = sqlite3.connect(f"{w}/gw/library/database.db")
bid = c.execute("SELECT book_id FROM books WHERE lib_id=?", (lib,)).fetchone()[0]
c.execute("UPDATE books SET abs_id='abs-test' WHERE book_id=?", (bid,))
c.execute("INSERT OR REPLACE INTO book_hashes(kobo_hash,book_id,first_seen) VALUES (?,?,datetime('now'))", (h.hexdigest(), bid))
c.commit(); print("paired", lib, "to", h.hexdigest())
PY
cat > "$W/home/.config/koreader/settings/kosync.lua" <<'LUA'
return { ["settings"] = { ["auto_sync"] = true, ["checksum_method"] = 0,
  ["custom_server"] = "http://host.docker.internal:3099", ["pages_before_update"] = 10,
  ["sync_backward"] = 1, ["sync_forward"] = 2,
  ["userkey"] = "4c5c29ba37e0eacf842c160844d04b9a", ["username"] = "kosim" } }
LUA
printf 'return {\n    ["wifi_enable_action"] = "turn_on",\n    ["quickstart_shown_version"] = 202607010000,\n}\n' > "$W/home/.config/koreader/settings.reader.lua"
echo "ready in $W"
