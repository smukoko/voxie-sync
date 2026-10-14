"""The one-click install on a Kobo that already runs KOReader.

Someone who installed KOReader themselves (with KFMon, an older version,
their own sync server and plugins) must be able to add Voxie with the same
one click, and keep what they had: no KOReader downloaded over theirs, no
second launcher, their plugins and settings untouched -- except Voxie's own
keys, and Binary document matching, without which nothing would ever pair.
"""
import json, os, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from click.testing import CliRunner  # noqa: E402
import libra_cli.main as cli_main  # noqa: E402
from libra_cli.reading_profile import _load  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


kobo = tempfile.mkdtemp()
os.makedirs(os.path.join(kobo, ".kobo"))
put(kobo, ".adds/koreader/git-rev", "v2025.10\n")
launcher = put(kobo, ".adds/koreader/koreader.sh", "#!/bin/sh\n# the user's own KOReader\n")
kfmon = put(kobo, ".adds/kfmon/config/koreader.ini", "[watch]\nfilename = /mnt/onboard/koreader.png\n")
put(kobo, ".adds/koreader/plugins/SSH.koplugin/main.lua", "-- ssh\n")
put(kobo, ".adds/koreader/settings/kosync.lua", """-- ./settings/kosync.lua
return {
    ["settings"] = {
        ["custom_server"] = "https://sync.koreader.rocks",
        ["checksum_method"] = 1,
        ["sync_forward"] = 3,
        ["whisper_backward"] = 2,
    },
}
""")
put(kobo, ".adds/koreader/settings.reader.lua", """-- ./settings.reader.lua
return {
    ["cre_font"] = "Bookerly",
    ["lastfile"] = "/mnt/onboard/mine.epub",
}
""")

cfg = put(tempfile.mkdtemp(), ".librasync.json", json.dumps({
    "GATEWAY_IP": "voxie.tail1.ts.net", "GATEWAY_URL": "https://voxie.tail1.ts.net",
    "ACCOUNT_USERNAME": "reader", "ACCOUNT_PASSWORD": "pw", "UPLOAD_TOKEN": "t"}))


def no_download(*a, **k):
    raise AssertionError("downloaded something onto a Kobo that already has KOReader")


with mock.patch.object(cli_main, "IS_MAC", True), \
     mock.patch.object(cli_main, "CONFIG_FILE", cfg), \
     mock.patch.object(cli_main, "find_kobo_mount", return_value=kobo), \
     mock.patch.object(cli_main, "download_pinned", no_download), \
     mock.patch.object(cli_main, "flush_volume", lambda p: None), \
     mock.patch.object(cli_main.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")), \
     mock.patch("libra_cli.reading_profile.default_backup_dir", return_value=tempfile.mkdtemp()), \
     mock.patch("libra_cli.fat_check.check", return_value=mock.Mock(ok=True, reason="", __str__=lambda s: "stub")):
    # subprocess.run is a stub here, so the FAT gate (hdiutil, Docker) cannot
    # run; the gate is tested on its own (tools/test_fat_check.py).
    res = CliRunner().invoke(cli_main.cli, ["setup-kobo", "--json", "--dicts", "none", "--no-fonts"])

out = [l for l in res.output.splitlines() if l.startswith("{")]
result = json.loads(out[-1]) if out else {}
check("the one click succeeds on a Kobo that already runs KOReader",
      result.get("success") is True and result.get("koreaderExisting") is True,
      (res.output[-400:] if not result.get("success") else ""))
check("no look asked for: the reader's own look is kept",
      result.get("look") == {"look": "koreader", "was": "koreader"}, result.get("look"))
check("their KOReader is not downloaded over or replaced",
      open(launcher).read().endswith("the user's own KOReader\n")
      and open(os.path.join(kobo, ".adds/koreader/git-rev")).read() == "v2025.10\n")
check("no second launcher: NickelMenu is not added beside their KFMon",
      not os.path.exists(os.path.join(kobo, ".kobo/KoboRoot.tgz"))
      and not os.path.exists(os.path.join(kobo, ".adds/nm")) and os.path.exists(kfmon))
check("an older KOReader gets a warning, not a refusal", "tested with KOReader" in res.output)

plugin = open(os.path.join(kobo, ".adds/koreader/plugins/librarian_sync.koplugin/main.lua")).read()
check("the Voxie plugin is added, set to the gateway",
      'local GATEWAY = ("https://voxie.tail1.ts.net")' in plugin)
check("...with the landing rules beside it (offline translation)", os.path.exists(
      os.path.join(kobo, ".adds/koreader/plugins/librarian_sync.koplugin/voxie_landing.lua")))
check("their other plugins are still there", os.path.exists(os.path.join(kobo, ".adds/koreader/plugins/SSH.koplugin/main.lua")))

ks = _load(os.path.join(kobo, ".adds/koreader/settings/kosync.lua"))["settings"]
check("Progress sync now points at Voxie", ks["custom_server"] == "https://voxie.tail1.ts.net")
check("document matching is Binary, which the gateway pairs by", ks["checksum_method"] == 0)
check("their own Progress sync preferences survive", ks.get("whisper_backward") == 2)
check("and they are told what changed",
      "sync.koreader.rocks" in res.output and "Binary" in res.output)

settings = _load(os.path.join(kobo, ".adds/koreader/settings.reader.lua"))
check("their chosen font and history survive", settings.get("cre_font") == "Bookerly"
      and settings.get("lastfile") == "/mnt/onboard/mine.epub")
check("their plugins are not tucked away (SSH, terminal... may be theirs)",
      not settings.get("plugins_disabled"), settings.get("plugins_disabled"))

# --- the Kobo's bottom-bar button ------------------------------------------------
check("a launcher that is not ours (their KFMon, no NickelMenu file) is left alone",
      not os.path.exists(os.path.join(kobo, ".adds/nm/koreader"))
      and not os.path.exists(os.path.join(kobo, ".adds/voxie")))

mine = tempfile.mkdtemp()      # a Kobo an earlier Voxie set up: the plain "KOReader" entry
put(mine, ".adds/nm/koreader",
    "menu_item:main:KOReader:cmd_spawn:quiet:exec /mnt/onboard/.adds/koreader/koreader.sh\n")
check("an earlier Voxie entry is recognised as ours", cli_main.nickelmenu_is_ours(mine))
cli_main.write_nickelmenu(mine, ROOT)
nm = open(os.path.join(mine, ".adds/nm/koreader")).read()
check("it becomes the Voxie button, with its icon", "menu_main_15505_label:Voxie" in nm
      and "menu_main_15505_icon:/mnt/onboard/.adds/voxie/nav.png" in nm
      and "menu_main_15505_icon_active:/mnt/onboard/.adds/voxie/navon.png" in nm)
check("the entry inside still says it is KOReader", "menu_item:main:Voxie (KOReader):cmd_spawn" in nm)
icons = sorted(os.listdir(os.path.join(mine, ".adds/voxie")))
check("both icons are written, with 8.3-safe names", icons == ["nav.png", "navon.png"]
      and all(open(os.path.join(mine, ".adds/voxie", i), "rb").read(8) == b"\x89PNG\r\n\x1a\n" for i in icons))
check("the same file is edited in place, no second NickelMenu file",
      os.listdir(os.path.join(mine, ".adds/nm")) == ["koreader"])
check("and it is still ours on the next run", cli_main.nickelmenu_is_ours(mine))

theirs = tempfile.mkdtemp()
put(theirs, ".adds/nm/koreader", "menu_item:main:KOReader:cmd_spawn:quiet:exec /mnt/onboard/.adds/koreader/koreader.sh\n"
    "menu_item:main:Dump Syslog:cmd_spawn:logread > /mnt/onboard/syslog.log\n")
check("a NickelMenu file with the user's own items is not ours", not cli_main.nickelmenu_is_ours(theirs))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
