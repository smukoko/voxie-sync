#!/bin/bash
# Install Voxie on a Mac or a Linux computer (Debian/Ubuntu):
#
#   curl -fsSL https://raw.githubusercontent.com/smukoko/voxie-sync/main/install.sh | bash
#
# Installs what Voxie needs (Python, ffmpeg, espeak-ng, kepubify, Docker), puts
# Voxie in ~/voxie with its own Python environment, and the `voxie` command on
# your PATH. Then: `voxie init`. Safe to run again: it updates.
#
# Piped into bash, this script's stdin is the script itself: it must never
# read from stdin (no prompts), or it would eat its own next lines.
set -euo pipefail

REPO="${VOXIE_REPO:-https://github.com/smukoko/voxie-sync.git}"
DIR="${VOXIE_DIR:-$HOME/voxie}"
# kepubify 4.0.4 for Linux, pinned with the sha256 of each release asset
# (Homebrew pins it on a Mac). It turns the EPUB into the KEPUB a Kobo reads.
KEPUBIFY_VERSION="v4.0.4"
KEPUBIFY_SHA_X86_64="37d7628d26c5c906f607f24b36f781f306075e7073a6fe7820a751bb60431fc5"
KEPUBIFY_SHA_ARM64="5a15b8f6f6a96216c69330601bca29638cfee50f7bf48712795cff88ae2d03a3"

say() { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
Darwin)
    command -v brew >/dev/null || die "Install Homebrew first (https://brew.sh), then run this line again."
    say "Installing Python, ffmpeg, espeak-ng, kepubify and git (Homebrew) ..."
    brew install python@3.12 ffmpeg espeak-ng kepubify git
    if ! command -v docker >/dev/null; then
        say "Installing Docker Desktop (Voxie uses it to align the narration with the text) ..."
        brew install --cask docker
        DOCKER_NOTE="Open Docker Desktop once (Applications) and let it finish starting."
    fi
    PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
    BIN_DIR="$(brew --prefix)/bin"
    ;;
Linux)
    command -v apt-get >/dev/null || die "This installer knows Debian and Ubuntu. Elsewhere, install Python 3.10-3.12, ffmpeg, espeak-ng, kepubify and Docker, then follow docs/GUIDE.md."
    say "Installing Python, ffmpeg, espeak-ng, git and Docker (apt; asks for your password) ..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg espeak-ng git curl docker.io
    if ! id -nG "$USER" | grep -qw docker; then
        sudo usermod -aG docker "$USER"
        DOCKER_NOTE="Log out and back in once, so you may use Docker without sudo."
    fi
    mkdir -p "$HOME/.local/bin"
    case "$(uname -m)" in
        x86_64) asset=kepubify-linux-64bit; sha=$KEPUBIFY_SHA_X86_64 ;;
        aarch64|arm64) asset=kepubify-linux-arm64; sha=$KEPUBIFY_SHA_ARM64 ;;
        *) die "No kepubify build for $(uname -m)." ;;
    esac
    if ! command -v kepubify >/dev/null; then
        say "Installing kepubify $KEPUBIFY_VERSION (checksum verified) ..."
        tmp="$(mktemp)"
        curl -fsSL -o "$tmp" "https://github.com/pgaskin/kepubify/releases/download/$KEPUBIFY_VERSION/$asset"
        echo "$sha  $tmp" | sha256sum -c --quiet - || die "kepubify's checksum does not match: not installed."
        install -m 755 "$tmp" "$HOME/.local/bin/kepubify" && rm -f "$tmp"
    fi
    PYTHON=python3
    BIN_DIR="$HOME/.local/bin"
    ;;
*)
    die "Windows is not supported yet. Voxie runs on a Mac or on Linux; any KOReader device can still pair with a Voxie server by hand (docs/KOREADER_DEVICES.md)."
    ;;
esac

if [ -d "$DIR/.git" ]; then
    say "Updating Voxie in $DIR ..."
    git -C "$DIR" pull --ff-only
else
    say "Downloading Voxie into $DIR ..."
    git clone "$REPO" "$DIR"
fi

say "Installing Voxie's Python packages (a few minutes the first time) ..."
[ -d "$DIR/.venv" ] || "$PYTHON" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR" -r "$DIR/requirements.txt"
if [ ! -f "$DIR/.env" ]; then
    cp "$DIR/.env.example" "$DIR/.env"
    chmod 600 "$DIR/.env"
fi
ln -sf "$DIR/.venv/bin/voxie" "$BIN_DIR/voxie"

say "✅ Voxie is installed."
[ -n "${DOCKER_NOTE:-}" ] && echo "   $DOCKER_NOTE"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "   In a new terminal, $BIN_DIR is on your PATH: the voxie command works there." ;; esac

# Straight on to the setup: it opens the setup page in the browser (or asks in
# the terminal when there is no browser). Its input is the terminal, never
# this script's own stdin. VOXIE_NO_SETUP=1 stops here instead.
if [ -z "${VOXIE_NO_SETUP:-}" ] && (exec </dev/tty) 2>/dev/null; then
    say "Opening the setup ..."
    exec "$DIR/.venv/bin/voxie" init </dev/tty
fi
echo "   Next:  voxie init"
