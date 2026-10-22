import os
import time
import sys
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
import hashlib
import shutil
import subprocess
import zipfile
import requests
import traceback
import builtins
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Paths only; this module reads no configuration at import.
from core import failure_report, library, platform_support, progress

# 🚨 ANTI-BLACK-BOX: Force Python to instantly flush prints to the screen
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# --- MAC CONFIGURATION ---
DROPZONE = os.path.expanduser("~/Kobo_Dropzone")
# A second folder rather than a flag: dropping a file is the whole interface,
# so "which folder" is the only way to express intent without inventing one.
DROPZONE_NO_AUDIO = os.path.expanduser("~/Kobo_Dropzone_NoAudio")
ARCHIVE = os.path.expanduser("~/Kobo_Archive")
# Where a book goes when its run did not finish. Not watched: a book left in
# the drop folder was narrated again by the next start (process_backlog), and
# a new run begins by wiping mfa_workspace -- the only copy of the narration
# when the chunk cache is off (Twisted, 2026-10-11) -- and bills the GPU again,
# at every restart, for a book that fails every time.
FAILED = os.path.expanduser("~/Kobo_Dropzone_Failed")
CONFIG_FILE = os.path.expanduser("~/.librasync.json")


def cannot_start(message):
    """Say why the watcher cannot start, on screen as well as in a log nobody
    reads, and exit. launchd restarts it every few seconds, so the page opens
    at most once an hour (it is rewritten each time)."""
    print(f"❌ FATAL: {message}")
    flag_problem("Voxie could not start", message, "Voxie watcher", "Voxie's watcher could not start",
                 key="startup", reopen_after=3600)
    sys.exit(1)


def load_gateway_config():
    if not os.path.exists(CONFIG_FILE):
        cannot_start(f"Config not found at {CONFIG_FILE}. Run 'voxie init' first.")
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    gateway_ip = config.get("GATEWAY_IP")
    if not gateway_ip:
        cannot_start("No GATEWAY_IP found in config. Run 'voxie init' first.")
    upload_token = config.get("UPLOAD_TOKEN")
    if not upload_token:
        # Nothing here can recover: the gateway rejects tokenless uploads, and a
        # book would be generated on a paid GPU only to be refused on arrival.
        cannot_start("No UPLOAD_TOKEN found in config. Re-run 'voxie init'.")
    from core.gateway_address import abs_url, gateway_url
    return f"{gateway_url(config)}/upload", upload_token, abs_url(config), config.get("ABS_API_TOKEN")


# Read when the watcher starts (configure()), never at import: importing this
# module used to exit the process when ~/.librasync.json was missing, so any
# test or tool importing it passed only on a machine that had been set up --
# CI on a fresh Ubuntu runner found it.
DO_SERVER_URL = UPLOAD_TOKEN = ABS_URL = ABS_API_TOKEN = None


def configure():
    global DO_SERVER_URL, UPLOAD_TOKEN, ABS_URL, ABS_API_TOKEN
    DO_SERVER_URL, UPLOAD_TOKEN, ABS_URL, ABS_API_TOKEN = load_gateway_config()


def trigger_abs_scan():
    """Ask Audiobookshelf to rescan after a book lands.

    Without this the library keeps serving the *previous* audio for a
    regenerated book until ABS happens to scan on its own. Observed 2026-09-14:
    a re-run replaced The Cask of Amontillado with an 859.8s narration, the
    upload succeeded and the gateway reported no orphans, yet ABS still
    advertised the superseded 814.3s file -- with a two-day-old mtime -- for
    minutes afterwards. A forced scan corrected it immediately.

    That matters beyond cosmetics: "the ABS item's duration matches the map's"
    is the assertion this project makes after every run, and a stale scan makes
    it read as a mismatch when nothing is wrong. Better to remove the ambiguity
    than to teach ourselves to ignore the check.

    Deliberately best-effort. The bytes are already safely on the gateway by the
    time this runs, so a failure here costs a slower refresh and nothing else --
    it must never turn a successful upload into a failed one.
    """
    if not ABS_API_TOKEN:
        return
    base = ABS_URL
    headers = {"Authorization": f"Bearer {ABS_API_TOKEN}"}
    try:
        libs = requests.get(f"{base}/api/libraries", headers=headers, timeout=20).json()
        for lib in libs.get("libraries", []):
            requests.post(f"{base}/api/libraries/{lib['id']}/scan",
                          headers=headers, timeout=30)
        print("   🔄 Asked Audiobookshelf to rescan.")
    except Exception as e:
        print(f"   ⚠️ Could not trigger an ABS rescan ({str(e)[:80]}). "
              "The book is uploaded; the library will catch up on its own.")

os.makedirs(DROPZONE, exist_ok=True)
os.makedirs(DROPZONE_NO_AUDIO, exist_ok=True)
os.makedirs(ARCHIVE, exist_ok=True)

UPLOAD_ATTEMPTS = 4


PART_SIZE = 8 * 1024 * 1024   # small enough that losing one costs little


def _base_url():
    """Strip the /upload suffix so the resumable routes can be addressed."""
    return DO_SERVER_URL[: -len("/upload")] if DO_SERVER_URL.endswith("/upload") else DO_SERVER_URL


def _pack_fingerprint(path):
    """sha256 of the pack, and the id derived from it.

    Derived from content rather than the filename, so an interrupted upload
    resumes onto the same parts on the server even if the pack is rebuilt --
    and a pack whose contents changed correctly starts over instead of being
    silently stitched together from two different books.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    digest = h.hexdigest()
    return digest, digest[:32]


def upload_resumable(zip_filename):
    """Upload in parts, sending only what the server does not already hold.

    Returns True on success, False on a definite failure, and None when the
    server has no resumable routes -- in which case the caller falls back to
    sending the whole body, so an old gateway keeps working.
    """
    headers = {"X-Libra-Token": UPLOAD_TOKEN}
    size = os.path.getsize(zip_filename)
    total_parts = max(1, (size + PART_SIZE - 1) // PART_SIZE)
    sha, pack_id = _pack_fingerprint(zip_filename)

    try:
        r = requests.get(f"{_base_url()}/upload/status/{pack_id}", headers=headers, timeout=(15, 60))
    except requests.exceptions.RequestException as e:
        print(f"\n⚠️  Could not reach the gateway: {e}")
        return False
    if r.status_code == 404:
        return None                      # gateway predates resumable uploads
    if r.status_code == 401:
        print("❌ FATAL [PHASE 3]: Gateway rejected the upload token.")
        return False
    if r.status_code != 200:
        return None

    have = set(r.json().get("parts", []))
    if have:
        print(f"   ↩️  Resuming: {len(have)}/{total_parts} parts already on the server.")

    with open(zip_filename, "rb") as f:
        for index in range(total_parts):
            if index in have:
                continue
            f.seek(index * PART_SIZE)
            payload = f.read(PART_SIZE)
            part_headers = dict(headers)
            part_headers.update({
                "X-Pack-Id": pack_id,
                "X-Part-Index": str(index),
                "X-Part-Sha256": hashlib.sha256(payload).hexdigest(),
                "Content-Type": "application/octet-stream",
            })
            try:
                pr = requests.post(f"{_base_url()}/upload/part", data=payload,
                                   headers=part_headers, timeout=(15, 300))
            except requests.exceptions.RequestException as e:
                print(f"\n⚠️  Part {index + 1}/{total_parts} failed: {e}")
                print("   -> Parts already sent are kept; the next attempt resumes here.")
                return False
            if pr.status_code != 200:
                print(f"\n❌ Part {index + 1}/{total_parts} rejected: {pr.status_code} {pr.text[:200]}")
                return False
            done = index + 1
            sys.stdout.write(f"\r   📤 Streaming to Warehouse: {done * 100 / total_parts:5.1f}% "
                             f"({done}/{total_parts} parts)")
            sys.stdout.flush()
    print("")

    try:
        cr = requests.post(f"{_base_url()}/upload/commit", headers=headers, timeout=(15, 1800),
                           json={"pack_id": pack_id, "total_parts": total_parts,
                                 "sha256": sha, "filename": os.path.basename(zip_filename)})
    except requests.exceptions.RequestException as e:
        print(f"\n⚠️  Commit failed: {e}. Parts are kept; the next attempt resumes.")
        return False
    if cr.status_code == 200:
        print("✅ [PHASE 3] Upload complete! Cloud Warehouse accepted it.")
        return True
    print(f"❌ [PHASE 3]: Commit rejected: {cr.status_code} {cr.text[:300]}")
    return False


def upload_with_retries(zip_filename):
    """POST a Sync Pack to the gateway, retrying transient network failures.

    The upload crosses the home/mobile uplink and takes minutes at book size,
    so a single dropped connection is an ordinary event rather than an
    exceptional one -- it is not evidence the gateway is down. Returns True
    once the gateway has accepted the pack.
    """
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        if attempt > 1:
            delay = 15 * (2 ** (attempt - 2))
            print(f"   ⏳ Retry {attempt}/{UPLOAD_ATTEMPTS} in {delay}s...")
            time.sleep(delay)
        try:
            resumed = upload_resumable(zip_filename)
            if resumed is True:
                return True
            if resumed is False:
                continue            # keep the parts, retry resumes from them

            with open(zip_filename, 'rb') as f:
                encoder = MultipartEncoder(
                    fields={'file': (os.path.basename(zip_filename), f, 'application/zip')}
                )
                monitor = MultipartEncoderMonitor(encoder, create_callback(encoder))
                headers = {
                    'Content-Type': monitor.content_type,
                    'X-Libra-Token': UPLOAD_TOKEN,
                }
                response = requests.post(DO_SERVER_URL, data=monitor,
                                         headers=headers, timeout=(15, 600))
            print("")

            if response.status_code == 200:
                print("✅ [PHASE 3] Upload complete! Cloud Warehouse accepted it.")
                return True
            if response.status_code == 401:
                print("❌ FATAL [PHASE 3]: Gateway rejected the upload token.")
                print("   -> UPLOAD_TOKEN in ~/.librasync.json must match UPLOAD_TOKEN in the server's .env.")
                return False       # retrying a bad token just wastes the bandwidth
            if response.status_code == 503:
                print("❌ FATAL [PHASE 3]: Gateway has no UPLOAD_TOKEN set, so uploads are disabled.")
                print("   -> Set UPLOAD_TOKEN in the server's .env and restart it (docker compose up -d).")
                return False
            print(f"❌ [PHASE 3]: Server rejected upload. Status {response.status_code}")
            print(f"   -> Server Response: {response.text[:500]}")
            if 400 <= response.status_code < 500:
                return False       # a malformed pack will be malformed next time too
        except requests.exceptions.Timeout:
            print("\n⚠️  [PHASE 3]: Connection timed out mid-upload.")
        except requests.exceptions.ConnectionError as e:
            print(f"\n⚠️  [PHASE 3]: Connection dropped mid-upload: {e}")
        except Exception as e:
            print(f"\n🚨 UPLOAD CRASH: {e}")
            return False

    print(f"❌ [PHASE 3]: Gave up after {UPLOAD_ATTEMPTS} attempts.")
    return False


def create_callback(encoder):
    encoder_len = encoder.len
    def callback(monitor):
        percent = (monitor.bytes_read / encoder_len) * 100
        # \r overwrites the line, so the transfer reads as one live counter.
        sys.stdout.write(f"\r   📤 Streaming to Warehouse: {percent:.1f}% "
                         f"({monitor.bytes_read // (1024*1024)}MB / {encoder_len // (1024*1024)}MB)")
        sys.stdout.flush()
    return callback


def retry_pending_uploads():
    """Push any Sync Pack left behind by an upload that failed earlier."""
    pending = sorted(f for f in os.listdir(".") if f.endswith("_SyncPack.zip"))
    if not pending:
        return
    print(f"📦 Found {len(pending)} Sync Pack(s) from a failed upload. Retrying...")
    for name in pending:
        print(f"\n🚀 Retrying upload of {name} ({os.path.getsize(name) // 1024 // 1024} MB)...")
        if upload_with_retries(name):
            os.remove(name)
            print("🗑️ Deleted local Sync Pack.")
            trigger_abs_scan()
        else:
            print(f"   -> {name} kept for the next attempt.")
            flag_problem("Voxie could not send a finished book",
                         f"{name} is a finished audiobook that could not be uploaded to your server. "
                         "It stays on this computer and is tried again at every start. Check that "
                         "your server is running and that this computer reaches it.",
                         name, "A finished book was not sent")


# Held for one book, not the process (core/platform_support.keep_awake: the
# engine holds it too when it is run by itself).
keep_awake = platform_support.keep_awake


# Members whose bytes are already compressed, and are therefore stored rather
# than deflated into the Sync Pack.
#
# Measured on six of Screwtape's chapters: 38.8MB of MP3 deflated to 37.8MB --
# 2.6% -- and took 1.29s against 0.07s stored. A sixteen-hour book is ~1.4GB of
# MP3, so deflating it is about 45 seconds of this Mac's CPU to save ~36MB,
# which is ~14 seconds of its uplink (extrapolated from the same measurement).
# That is the wrong way round, and it is serial time in the middle of a run.
#
# Per member rather than a blanket ZIP_STORED, because the manifest goes the
# other way: 5.7MB of word timings deflate to 0.9MB in a tenth of a second, and
# a sixteen-hour novel's manifest is far larger than Screwtape's. Storing that
# would give back more than the audio saves.
ALREADY_COMPRESSED = {
    ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus",
    ".epub", ".kepub", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp",
}


def pack_write(zipf, file_path, arcname=None):
    """Add one file to a pack, compressing it only if that can pay for itself."""
    ext = os.path.splitext(file_path)[1].lower()
    compress = zipfile.ZIP_STORED if ext in ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
    zipf.write(file_path, arcname or file_path, compress_type=compress)


EBOOK_READY_MARKER = os.path.join("library", ".ebook_ready.json")


def ship_ebook_early(marker_path):
    """Upload the readable book while its audio is still being generated.

    The engine finalises the kepub, the watermark and the database row before it
    deploys a pod, so everything a reader needs exists about two minutes into a
    run that takes two hours. Waiting for the audio meant the book was
    unreadable for the whole time it was being narrated, and a five-hour run
    that died at hour four delivered nothing at all.

    Best-effort by construction. A failure here costs the early read and nothing
    else: the same book ships complete at the end of the run, which is what
    happened before this existed. So it never returns a failure the caller has
    to handle, and never touches the EPUB in the dropzone -- archiving stays the
    business of the full upload, because the run is not finished yet.
    """
    try:
        with open(marker_path) as f:
            marker = json.load(f)
        shipped_lib_id = marker.get("lib_id")
        ebook_dir = marker["ebook_dir"]
        title = marker.get("title", os.path.basename(ebook_dir))
        if not os.path.isdir(ebook_dir):
            return

        zip_filename = f"{os.path.basename(ebook_dir)}_EbookPack.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(ebook_dir):
                for file in files:
                    # The ebook shares its folder with the previous run's audio.
                    # An early pack carrying that audio would look like a
                    # finished narration to the gateway and replace the real one.
                    if not library.is_audio(file):
                        pack_write(zipf, os.path.join(root, file))

        size_kb = os.path.getsize(zip_filename) // 1024
        print(f"\n📖 [EARLY] {title} is readable — shipping the ebook now "
              f"({size_kb} KB) while the audio is made.")
        if upload_with_retries(zip_filename):
            print("📖 [EARLY] Ebook delivered. It can be read on the Kobo now; "
                  "the position will map onto the audio when it arrives.")
            os.remove(zip_filename)
            return shipped_lib_id
        else:
            # Do not keep this one for the retry sweep. The full pack is coming
            # and supersedes it, and a stale *_EbookPack.zip left in the
            # directory would be re-uploaded on the next start as if it were an
            # unfinished book.
            print("📖 [EARLY] Ebook upload failed; it ships with the finished book instead.")
            os.remove(zip_filename)
    except Exception as e:
        print(f"📖 [EARLY] Skipped the early ebook publish ({e}).")
    finally:
        try:
            os.remove(marker_path)
        except OSError:
            pass
    return None


# librerian_engine_v3.NO_VOICE_EXIT, repeated rather than imported: importing
# the engine would load its whole dependency stack into the watcher.
NO_VOICE_EXIT = 4
# librerian_engine_v3.NOT_ENGLISH_EXIT: the book is not English, so the engine
# published it as an ebook only; it is packed and uploaded like a NoAudio drop.
NOT_ENGLISH_EXIT = 5


# A desktop notification (osascript on a Mac, notify-send on Linux). The
# watcher runs as a background service, where nobody reads its log, so anything
# the user must act on goes here too.
notify = platform_support.notify


def set_aside(filepath, why, report=True):
    """Move a book whose run did not finish out of the drop folder, and say so.

    A failure opens the report page (core/failure_report.py) so the user sees
    what happened and can ask for help; `report=False` is for a book that is
    only waiting on a setup step (no narrator voice), where the notice says it.

    Returns where it went, or None if it could not be moved (then it stays,
    and the next start narrates it again, as before).
    """
    name = os.path.basename(filepath)
    dest = os.path.join(FAILED, name)
    try:
        os.makedirs(FAILED, exist_ok=True)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            dest = os.path.join(FAILED, f"{stem} ({time.strftime('%Y-%m-%d %H.%M.%S')}){ext}")
        shutil.move(filepath, dest)
    except OSError as e:
        print(f"   ⚠️ Could not move it out of the drop folder ({e}); the next start narrates it again.")
        return None
    print(f"   📁 Moved to {FAILED}, so it is not narrated again by itself. Drop it back in to try again.")
    notify(f"Voxie could not finish '{os.path.splitext(name)[0]}'",
           f"{why} It is in Kobo_Dropzone_Failed; drop it into Kobo_Dropzone again to retry.")
    if report:
        failure_report.show_failure(
            name, f"{why} The book is in Kobo_Dropzone_Failed; drop it back into Kobo_Dropzone to try again.")
    return dest


def friendly_name(filename):
    """"Winning The Unforgiving Race to Greatness" from a Z-Library style file name
    ("... (Tim S. Grover) (z-library.sk, 1lib.sk, z-lib.sk).epub"): what a notice says."""
    import re
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", os.path.splitext(filename)[0])
    return re.sub(r"\s+", " ", name).strip() or filename


def tell_progress(launched, told):
    """Desktop notices for what the engine wrote to the progress file since
    `launched` (a stale file from the last book is not this book's news)."""
    status = progress.read()
    if not status or status.get("updated", 0) < launched:
        return
    for _, title, message in progress.notice(status, told):
        notify(title, message)


def flag_problem(title, message, name, headline, key="run", reopen_after=failure_report.REOPEN_AFTER):
    """A problem that is not a book set aside (a failed upload, holes in a
    narration, a watcher that cannot start): the notice, and the report page
    so the user can see why and ask for help. Never raises."""
    notify(title, message)
    failure_report.show_failure(name, message, headline=headline, key=key, reopen_after=reopen_after)


def report_failed(lib_id):
    """Tell the gateway a published book's audio will not arrive.

    The early publish marks it 'pending'; without this a run that died left it
    'pending' forever, indistinguishable from one still being narrated.
    Best-effort: the book stays readable either way.
    """
    try:
        r = requests.post(f"{_base_url()}/books/{lib_id}/state", json={"state": "failed"},
                          headers={"X-Libra-Token": UPLOAD_TOKEN}, timeout=(15, 30))
        print(f"   📕 Gateway told the audio for {lib_id} failed ({r.status_code}).")
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️ Could not report the failure to the gateway ({e}).")


BUILD_MARKER = os.path.join("library", ".last_build.json")


def read_build_marker():
    """The folder the engine says it just built, or None to fall back.

    Only trusted if it names a directory that exists -- a stale marker from an
    earlier run is worse than no marker, because it would package the wrong
    book and upload it under this one's name.
    """
    try:
        with open(BUILD_MARKER) as f:
            folder = json.load(f).get("ebook_dir")
    except (OSError, ValueError):
        return None
    return folder if folder and os.path.isdir(folder) else None


class GhostHandler(FileSystemEventHandler):
    def __init__(self, no_audio=False):
        super().__init__()
        self.no_audio = no_audio

    def on_created(self, event):
        # 🚨 WRAP THE ENTIRE THREAD IN A CRASH NET
        try:
            if event.is_directory or not event.src_path.endswith('.epub'):
                return

            # A book-length run is a quarter of an hour at best, with no
            # keyboard activity for any of it, so the machine idles out
            # underneath it. Sleeping mid-run strands a RunPod pod that goes on
            # billing and drops the upload, so the assertion is held for exactly
            # as long as the ghost is working and released the moment it is not.
            with keep_awake():
                self._process(event.src_path)

        # 🚨 THE MAIN CRASH NET (This was missing!)
        except Exception as e:
            print(f"\n🚨 CRITICAL GHOST THREAD CRASH 🚨")
            print(traceback.format_exc())
            print("="*50)
            if os.path.exists(event.src_path):
                set_aside(event.src_path, "The watcher stopped with an error.")

    def _process(self, filepath):
        filename = os.path.basename(filepath)
        book_name = filename.replace('.epub', '')

        print(f"\n{'='*50}")
        print(f"👻 GHOST WOKE UP: Detected {filename}")
        print(f"{'='*50}")
        time.sleep(3)

        # --- PHASE 1: THE FACTORY ---
        print("⚙️ [PHASE 1] Firing up the ML Engine...")
        ghost_env = os.environ.copy()
        ghost_env["PATH"] = ":".join(platform_support.tool_path_prefix() + [ghost_env.get("PATH", "")])

        # sys.executable, not "python": under a LaunchAgent there is no
        # "python" on PATH -- launchd hands over a minimal environment with
        # no pyenv shims -- so the engine never started and the ghost died
        # with FileNotFoundError. Running by hand in a shell hid this,
        # because there the shim is on PATH. The engine also needs the same
        # interpreter as the watcher, since that is where the deps live.
        engine_cmd = [sys.executable, "-u", "librerian_engine_v3.py", filepath]
        if self.no_audio:
            engine_cmd.append("--no-audio")
            print("   📚 Catalog-only drop: no audiobook will be generated.")
        # Popen rather than run(), so the ebook can be shipped the moment the
        # engine says it exists instead of two hours later when the audio is
        # done. The engine is a separate process and keeps working throughout,
        # so the upload is done inline here -- there is nothing else for the
        # watcher to do while it waits, and a thread would buy nothing but a
        # race on the marker file.
        if os.path.exists(EBOOK_READY_MARKER):
            # A marker left by a run that was killed names a book this run is
            # not building. Shipping it would upload the wrong book.
            os.remove(EBOOK_READY_MARKER)

        launched = time.time()
        told = set()
        if not self.no_audio:
            notify(f"Voxie started on '{friendly_name(filename)}'",
                   "Reading the book first. You will be told when you can read it on your Kobo, "
                   "and how long the audiobook takes.")
        engine_proc = subprocess.Popen(engine_cmd, env=ghost_env)
        shipped_early = False
        early_lib_id = None
        while engine_proc.poll() is None:
            if not shipped_early and os.path.exists(EBOOK_READY_MARKER):
                # Let the engine finish writing it.
                time.sleep(0.5)
                early_lib_id = ship_ebook_early(EBOOK_READY_MARKER)
                shipped_early = True
                if early_lib_id and not self.no_audio:
                    notify(f"'{friendly_name(filename)}' is ready to read",
                           "Sync your Kobo (Wi-Fi on) and open it. The audiobook is being made; "
                           "you will be told how long it takes, and when it is ready.")
            tell_progress(launched, told)
            time.sleep(2)
        tell_progress(launched, told)

        if engine_proc.returncode == NO_VOICE_EXIT:
            # Not a crash: a fresh install has no narrator yet. The engine
            # already printed how to add one.
            print("🎙️  No narrator voice, so no audiobook this time.")
            notify("Voxie needs a narrator voice",
                   f"'{book_name}' is readable now. Add a voice (run `voxie init`, or "
                   "`voxie voice-add`), then drop the book again for its audiobook.")
            if early_lib_id:
                report_failed(early_lib_id)
            set_aside(filepath, "It needs a narrator voice first: run `voxie init` and add one.",
                      report=False)
            return
        if engine_proc.returncode == NOT_ENGLISH_EXIT:
            # Not a failure: Voxie narrates English only (core/languages.py).
            # The engine published the ebook; upload it like a NoAudio drop.
            notify("Voxie narrates English only",
                   f"'{book_name}' is not in English, so it goes to your Kobo as an ebook, "
                   "without an audiobook. Other languages are planned.")
        elif engine_proc.returncode != 0:
            print(f"❌ FATAL: The Engine crashed with code {engine_proc.returncode}. Aborting.")
            if early_lib_id:
                print("   📖 The ebook was already delivered, so the book is readable "
                      "even though its audio was not finished.")
                report_failed(early_lib_id)
            set_aside(filepath, "Its narration stopped with an error.")
            return

        # "Finished" is not "complete". The engine exits 0 on a book whose swarm
        # gave up on some chunks, because a book missing two of a hundred and
        # fifty is still worth having -- but it must never be announced as a
        # clean run. On 2026-09-15 this line said "successfully" over a
        # Screwtape Letters that was 2.4 minutes short, and that claim is what
        # made the hole invisible until someone read the log by hand.
        incomplete = sorted(
            f[:-len(".quarantined")] for f in os.listdir("mfa_workspace")
            if f.endswith(".quarantined")) \
            if os.path.isdir("mfa_workspace") and engine_proc.returncode == 0 else []
        if incomplete:
            print(f"‼️  [PHASE 1] Engine finished, but {len(incomplete)} chunk(s) were "
                  "never narrated:")
            for name in incomplete:
                print(f"      • {name}")
            print("   Uploading anyway -- a book with holes beats no book -- but this "
                  "one is NOT complete.")
            print("   Re-run it once the cause is fixed.")
            flag_problem(f"Voxie: '{book_name}' is not complete",
                         f"{len(incomplete)} part(s) of '{book_name}' were never narrated, so the "
                         "audiobook has gaps. It is uploaded anyway. Look at the log below for the "
                         "cause, then drop the book into Kobo_Dropzone again.",
                         filename, "An audiobook has gaps")
        else:
            print("✅ [PHASE 1] Engine finished successfully!")

        # --- PHASE 2: PACKAGING ---
        print(f"📦 [PHASE 2] Hunting for generated files...")

        # Ask the engine which folder it built rather than guessing. The guess
        # was "most recently modified directory under library/ebooks", which is
        # only correct while nothing else touches the library -- and raises
        # ValueError on an empty directory, landing in the crash net with a
        # message about max() rather than about the book.
        latest_ebook_folder = read_build_marker()
        if latest_ebook_folder is None:
            ebooks_dir = library.BOOKS
            all_folders = [os.path.join(ebooks_dir, d) for d in os.listdir(ebooks_dir)
                           if os.path.isdir(os.path.join(ebooks_dir, d))] if os.path.isdir(ebooks_dir) else []
            if not all_folders:
                print(f"❌ The engine reported success but {ebooks_dir} is empty. Aborting.")
                set_aside(filepath, "The engine finished but left no book to send.")
                return
            latest_ebook_folder = max(all_folders, key=os.path.getmtime)
            print("   ⚠️  No build marker; falling back to the newest folder by mtime.")
        actual_book_folder = os.path.basename(latest_ebook_folder)

        zip_filename = f"{actual_book_folder}_SyncPack.zip"
        print(f"   -> Found Folder: {actual_book_folder}. Building Sync Pack...")

        pack_started = time.time()
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # The book's one folder: kepub, manifest and audio (no database).
            for root, _, files in os.walk(latest_ebook_folder):
                for file in files:
                    pack_write(zipf, os.path.join(root, file))

        print(f"✅ [PHASE 2] Sync Pack built successfully "
              f"({os.path.getsize(zip_filename) // 1024 // 1024} MB "
              f"in {time.time() - pack_started:.0f}s)")

        # --- PHASE 3: SHIPPING ---
        print(f"🚀 [PHASE 3] Uploading to the gateway ({DO_SERVER_URL})...")

        if upload_with_retries(zip_filename):
            # Archive only once the bytes are safely on the gateway. The folder
            # is made again if someone deleted it: a failed move here leaves
            # the book in the dropzone, and the next start narrates it again.
            os.makedirs(ARCHIVE, exist_ok=True)
            shutil.move(filepath, os.path.join(ARCHIVE, filename))
            print(f"🧹 Cleaned up: Moved {filename} to Archive.")
            os.remove(zip_filename)
            print("🗑️ Deleted local Sync Pack.")
            trigger_abs_scan()
            if self.no_audio or engine_proc.returncode == NOT_ENGLISH_EXIT:
                notify(f"'{friendly_name(filename)}' is ready to read",
                       "It is on your server as an ebook. Sync your Kobo (Wi-Fi on) and open it.")
            elif not incomplete:
                # A book with holes was already flagged; "ready" would be false.
                progress.update("done", say=True)
                tell_progress(launched, told)
            # Consumed. Leaving it would let a later run that failed before
            # writing its own marker package this book again under that book's
            # name.
            if os.path.exists(BUILD_MARKER):
                os.remove(BUILD_MARKER)
        else:
            # Keep the pack. Deleting it here used to throw away a finished
            # book because a Wi-Fi hiccup interrupted one POST -- the audio
            # had already cost a GPU run, and the only way back was to
            # generate it again. It is retried on the next startup instead.
            print(f"\n📦 Sync Pack kept at {zip_filename} ({os.path.getsize(zip_filename) // 1024 // 1024} MB).")
            print("   -> It will be retried automatically next time the ghost starts.")
            print(f"   -> Or push it now:  python mac_ghost.py --retry-uploads")
            # The narration is finished and in the pack: archive the book, or
            # the next start sends the pack and then narrates the book again.
            os.makedirs(ARCHIVE, exist_ok=True)
            shutil.move(filepath, os.path.join(ARCHIVE, filename))
            print(f"   -> {filename} moved to the Archive: its audiobook is in the pack.")
            flag_problem(f"Voxie could not send '{book_name}'",
                         f"'{book_name}' is narrated, but the upload to your server failed. "
                         "The finished audiobook is kept on this computer and is sent again when the "
                         "watcher restarts (voxie start-ghost). Check that your server is running and "
                         "that this computer reaches it.",
                         filename, "A finished book was not sent")

        print(f"👻 Ghost going back to sleep.\n{'='*50}")

def process_backlog(handler, folder=DROPZONE):
    """Handle EPUBs that arrived while the watcher was not running.

    watchdog only reports creations it actually witnessed, so a book dropped
    while the agent was down sits in the folder forever and the tool looks
    broken. On success the engine moves the file to the archive, so anything
    still here genuinely has not been processed.
    """
    pending = [f for f in os.listdir(folder) if f.endswith(".epub")]
    if not pending:
        return
    print(f"📥 Found {len(pending)} book(s) waiting from before startup.")
    for name in pending:
        handler.on_created(
            type("Event", (), {"is_directory": False,
                               "src_path": os.path.join(folder, name)})()
        )


def sweep_orphaned_pods():
    """Terminate GPU pods a previous run left billing.

    `kill -9` on the engine runs no cleanup at all -- not finally, not atexit --
    so a killed run leaves its pods running. It has happened: four pods in one
    day were deployed and never terminated, one billing 26 minutes unnoticed at
    $1.09/hr. With POD_COUNT above 1 a single kill leaves that many.

    core.pod_pool records every pod at deploy time in a file precisely so that
    something outside the killed process can find them. Here is that something:
    the next drop pays off the last kill, with nobody having to remember.

    Best-effort by construction -- a sweep that fails must not stop the watcher
    from starting, because a ghost that will not start is worse than a pod that
    needs killing by hand.
    """
    try:
        import asyncio
        from core import pod_pool
        killed = asyncio.run(pod_pool.sweep_orphans())
        if killed:
            print(f"   💰 Terminated {len(killed)} orphaned pod(s) from a previous run.")
    except Exception as e:
        print(f"   ⚠️  Could not sweep for orphaned pods ({e}); "
              "check https://console.runpod.io/pods by hand.")


if __name__ == "__main__":
    configure()
    if "--sweep-pods" in sys.argv:
        # Terminate orphans without starting the watcher, for the case where a
        # run was killed and the next drop is not imminent.
        sweep_orphaned_pods()
        sys.exit(0)

    if "--retry-uploads" in sys.argv:
        # Push a pack left over from a failed upload without waiting for, or
        # restarting, the watcher.
        retry_pending_uploads()
        sys.exit(0)

    print(f"👻 Mac Ghost Watcher running invisibly on: {DROPZONE}")
    print(f"   📚 Catalog-only (no audiobook): {DROPZONE_NO_AUDIO}")
    full_handler = GhostHandler()
    catalog_handler = GhostHandler(no_audio=True)

    # Before anything else: money left running by a previous kill.
    sweep_orphaned_pods()
    retry_pending_uploads()
    process_backlog(full_handler, DROPZONE)
    process_backlog(catalog_handler, DROPZONE_NO_AUDIO)

    observer = Observer()
    observer.schedule(full_handler, DROPZONE, recursive=False)
    observer.schedule(catalog_handler, DROPZONE_NO_AUDIO, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
