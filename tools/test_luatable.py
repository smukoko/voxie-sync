"""libra_cli.luatable reads and writes KOReader's settings files exactly.

Checked against LuaJIT itself, the VM KOReader uses: what we write must load
there as the same data the original did. Includes the shape that broke the
first version -- KOReader writes a newline inside a string as a backslash
followed by a real line break.
"""
import os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from libra_cli import luatable  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


SAMPLE = '''-- ./settings.reader.lua
return {
    ["cre_font"] = "Literata",
    ["copt_h_page_margins"] = {
        [1] = 15,
        [2] = 15,
    },
    ["copt_font_base_weight"] = 0.5,
    ["negative"] = -1,
    ["flag"] = false,
    ["doc_props"] = {
        ["identifiers"] = "http://www.gutenberg.org/1952\\
LibrarianID:LBR-9bad2825f7d5",
        ["quote"] = "she said \\"no\\"",
    },
    ["servers"] = {
        [1] = {
            ["title"] = "Voxie",
            ["url"] = "http://10.0.0.9:3000/opds",
        },
    },
}
'''

t = luatable.loads(SAMPLE)
check("parses nested tables, numbers, floats, negatives and booleans",
      t["copt_h_page_margins"] == {1: 15, 2: 15} and t["copt_font_base_weight"] == 0.5
      and t["negative"] == -1 and t["flag"] is False)
check("a backslash + line break inside a string is a newline",
      t["doc_props"]["identifiers"] == "http://www.gutenberg.org/1952\nLibrarianID:LBR-9bad2825f7d5")
check("escaped quotes survive", t["doc_props"]["quote"] == 'she said "no"')
check("writing and reading back is lossless", luatable.loads(luatable.dumps(t)) == t)

if shutil.which("luajit"):
    d = tempfile.mkdtemp()
    a, b = os.path.join(d, "a.lua"), os.path.join(d, "b.lua")
    open(a, "w").write(SAMPLE)
    open(b, "w").write(luatable.dumps(t, header="./settings.reader.lua"))
    ser = ('local function s(v) if type(v)~="table" then return type(v)..":"..tostring(v) end '
           'local k={} for x in pairs(v) do k[#k+1]=x end table.sort(k,function(p,q) return tostring(p)<tostring(q) end) '
           'local o={} for _,x in ipairs(k) do o[#o+1]=tostring(x).."="..s(v[x]) end return "{"..table.concat(o,",").."}" end '
           f'print(s(dofile("{a}"))==s(dofile("{b}")))')
    r = subprocess.run(["luajit", "-e", ser], capture_output=True, text=True)
    check("LuaJIT loads what we write as the same data as the original", r.stdout.strip() == "true", r.stderr[:80])
else:
    print("skip  LuaJIT comparison (no luajit)")

print("=" * 60)
print(f"{len(results)} checks: {sum(results)} passed, {len(results) - sum(results)} FAILED")
sys.exit(0 if all(results) else 1)
