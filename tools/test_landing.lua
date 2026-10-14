-- The plugin's landing rules (voxie_landing.lua) against tools/landing_examples.json,
-- the same examples tools/test_landing.py runs against the gateway's Python.
--
--     luajit tools/test_landing.lua

package.path = "librarian_sync.koplugin/?.lua;" .. package.path
local L = require("voxie_landing")

local passed, failed = 0, 0
local function check(name, ok, detail)
    if ok then passed = passed + 1 else failed = failed + 1 end
    print((ok and "PASS  " or "FAIL  ") .. name .. (detail and ("  (" .. tostring(detail) .. ")") or ""))
end

local decode = dofile("tools/json_decode.lua")

local f = assert(io.open("tools/landing_examples.json"))
local ex = decode(f:read("*a"))
f:close()
local nodes, chapters = ex.map.nodes, ex.map.chapters

for _, case in ipairs(ex.to_page) do
    local got = L.landing_position(nodes, chapters, case.seconds)
    check(string.format("to page  %6.1fs: %s", case.seconds, case.why), got == case.position, got)
end

for _, case in ipairs(ex.to_audio) do
    local got, from_map = L.audio_time(nodes, chapters, case.position, case.percentage)
    check("to audio " .. case.position:sub(-24) .. ": " .. case.why,
          math.abs(got - case.seconds) < 1e-6 and from_map == case.from_map,
          tostring(got) .. ", from map " .. tostring(from_map))
end

for _, case in ipairs(ex.chapter_at) do
    local n, off = L.chapter_at(chapters, case.seconds)
    check("chapter at " .. case.seconds .. "s", n == case.chapter and math.abs(off - case.offset) < 1e-6,
          "chapter " .. n .. ", " .. off .. "s")
end

check("an empty map lands nowhere", L.landing_position({}, chapters, 10) == nil)

-- Lua only: how the menu shows and reads a time.
check("times show as an audiobook player shows them",
      L.format_time(872) == "14:32" and L.format_time(3723) == "1:02:03" and L.format_time(5) == "0:05")
check("times typed in any of those forms are read",
      L.parse_time("14:32") == 872 and L.parse_time(" 1:02:03 ") == 3723 and L.parse_time("90") == 90)
check("anything else is not a time",
      L.parse_time("") == nil and L.parse_time("12:3a") == nil and L.parse_time("1:2:3:4") == nil
      and L.parse_time("-5") == nil)

print(string.rep("=", 60))
print(string.format("%d checks: %d passed, %d FAILED", passed + failed, passed, failed))
os.exit(failed == 0 and 0 or 1)
