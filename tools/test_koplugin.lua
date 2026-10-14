-- Exercise librarian_sync.koplugin/main.lua under LuaJIT, off the device.
--
-- KOReader is LuaJIT, so this runs the plugin in the same VM with KOReader's
-- modules replaced by recording stubs. It proves the plugin loads, registers
-- where it claims, reads the gateway the way it claims, and moves the page
-- only when asked. It cannot prove what the Kobo's screen looks like -- that
-- still needs the device.
--
--     luajit tools/test_koplugin.lua

local passed, failed = 0, 0
local function check(name, ok, detail)
    if ok then passed = passed + 1 else failed = failed + 1 end
    print((ok and "✅ " or "❌ ") .. name .. (detail and ("  (" .. tostring(detail) .. ")") or ""))
end

-- ---- recording stubs for everything the plugin requires --------------------
local log = { shown = {}, events = {}, scheduled = {}, unscheduled = {}, timeouts = {}, requests = {}, stack = 0 }
local http_reply = { code = 200, body = "" }
local online = true

local function stub(name, value) package.preload[name] = function() return value end end

stub("ui/event", { new = function(_, name, arg) return { name = name, arg = arg } end })
stub("ui/widget/infomessage", { new = function(_, t) return t end })
stub("json", { decode = function(s)
    -- enough JSON for the gateway's flat answer
    local t = {}
    for k, v in s:gmatch('"([%w_]+)"%s*:%s*"([^"]*)"') do t[k] = v end
    for k, v in s:gmatch('"([%w_]+)"%s*:%s*([%d%.]+)') do t[k] = tonumber(v) end
    if not s:match("^%s*{") then error("not json") end
    return t
end, encode = function(t)
    local parts = {}
    for k, v in pairs(t) do
        parts[#parts + 1] = string.format("%q:%s", k, type(v) == "string" and string.format("%q", v) or tostring(v))
    end
    return "{" .. table.concat(parts, ",") .. "}"
end })
stub("ui/uimanager", {
    show = function(_, w) table.insert(log.shown, w.text) end,
    close = function() end,
    scheduleIn = function(_, secs, fn) table.insert(log.scheduled, { secs = secs, fn = fn }) end,
    unschedule = function(_, fn) table.insert(log.unscheduled, fn) end,
})
local WidgetContainer = {}
function WidgetContainer:extend(t) t.__index = t; return setmetatable(t, { __index = self }) end
stub("ui/widget/container/widgetcontainer", WidgetContainer)
stub("socket.http", { request = function(req)
    table.insert(log.requests, req.url)
    log.last_req = req
    log.last_headers = req.headers
    log.last_body = nil
    if req.source then
        local parts = {}
        while true do local c = req.source(); if not c then break end; parts[#parts + 1] = c end
        log.last_body = table.concat(parts)
    end
    log.bodies = log.bodies or {}
    log.bodies[#log.requests] = log.last_body
    log.methods = log.methods or {}
    log.methods[#log.requests] = req.method
    if http_reply.err then return nil, http_reply.err end
    req.sink(http_reply.body)
    return 1, http_reply.code, http_reply.headers or {}, "status"
end })
stub("logger", { info = function() end, warn = function() end, dbg = function() end })
stub("ltn12", {
    sink = { table = function(t) return function(chunk) if chunk then table.insert(t, chunk) end return 1 end end },
    source = { string = function(str) local done = false
        return function() if done then return nil end done = true; return str end end },
})
stub("socket", { skip = function(n, ...) return select(n + 1, ...) end })
stub("socketutil", {
    set_timeout = function(_, b, t) table.insert(log.timeouts, { b, t }) end,
    reset_timeout = function() table.insert(log.timeouts, "reset") end,
})
stub("ffi/util", { template = function(s, ...)
    local args = { ... }
    return (s:gsub("%%(%d)", function(i) return tostring(args[tonumber(i)]) end))
end })
stub("gettext", function(s) return s end)
stub("ui/network/manager", {
    isOnline = function() return online end,
    getConnectionState = function() return online end,
})
-- KOReader's data folder (.adds/koreader on a Kobo): a fresh temp folder.
local DATA = (os.getenv("TMPDIR") or "/tmp"):gsub("/+$", "") .. "/voxie_koplugin_" .. os.time() .. math.random(1e6)
os.execute("mkdir -p '" .. DATA .. "'")
stub("datastorage", { getDataDir = function() return DATA end })
stub("libs/libkoreader-lfs", {
    attributes = function(path, what)
        return os.execute("test -d '" .. path .. "'") == 0 and "directory" or nil
    end,
    mkdir = function(path) return os.execute("mkdir '" .. path .. "'") == 0 end,
})
-- KOReader's ffi/zlib, on the system's own libz, so the stored map is really inflated.
local ffi = require("ffi")
ffi.cdef[[ int uncompress(uint8_t *dest, unsigned long *destLen, const uint8_t *source, unsigned long sourceLen); ]]
local ok_z, libz = pcall(ffi.load, "z")
if not ok_z then libz = ffi.load("libz.so.1") end
stub("ffi/zlib", { zlib_uncompress = function(zdata, datalen)
    local buf = ffi.new("uint8_t[?]", datalen)
    local buflen = ffi.new("unsigned long[1]", datalen)
    assert(libz.uncompress(buf, buflen, zdata, #zdata) == 0)
    return ffi.string(buf, buflen[0])
end })
-- KOReader's ffi/sha2 is pure Lua; here Python's hashlib computes the same digest.
local function sha256(data)
    local path = DATA .. "/sha.in"
    local f = assert(io.open(path, "wb")); f:write(data); f:close()
    local h = io.popen("python3 -c \"import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())\" '" .. path .. "'")
    local out = h:read("*l"); h:close()
    return out
end
stub("ffi/sha2", { sha256 = sha256 })
stub("rapidjson", { decode = dofile("tools/json_decode.lua") })
local dialogs = {}
stub("ui/widget/multiinputdialog", { new = function(_, t)
    t.onShowKeyboard = function() end
    t.getFields = function() return t.answers end
    table.insert(dialogs, t)
    return t
end })
local queue = { items = {} }
function queue:load() return self.items end
function queue:save(items) self.items = items end
stub("KOSyncQueue", queue)
package.path = "librarian_sync.koplugin/?.lua;" .. package.path

-- ---- load the real plugin, as setup_kobo would write it --------------------
local f = assert(io.open("librarian_sync.koplugin/main.lua"))
local template = f:read("*a")
f:close()
local src = template:gsub("__GATEWAY_URL__", "http://10.0.0.9:3000")
check("one placeholder: the gateway's whole URL", not template:find("__GATEWAY_IP__")
      and not template:find("__CLOUD_PORT__") and select(2, template:gsub("__GATEWAY_URL__", "")) == 1)
local chunk, err = loadstring(src, "main.lua")
check("the plugin compiles under LuaJIT", chunk ~= nil, err)
local Plugin = chunk()

local function new_reader(percent)
    local ui = {
        document = { file = "/mnt/onboard/book.kepub.epub", info = { has_pages = false } },
        doc_settings = { readSetting = function(_, k) if k == "partial_md5_checksum" then return "abc123" end end },
        rolling = { getLastPercent = function() return percent end,
                    getLastProgress = function() return log.xpointer end },
        link = { addCurrentLocationToStack = function() log.stack = log.stack + 1 end },
        menu = { registerToMainMenu = function(_, p) log.registered = p end },
        handleEvent = function(_, e) table.insert(log.events, e) end,
        kosync = { settings = { username = "reader", userkey = "k3y" } },
    }
    local p = setmetatable({ ui = ui }, { __index = Plugin })
    p:init()
    return p
end

-- ---- registration --------------------------------------------------------------
check("reader-only, like KOSync", Plugin.is_doc_only == true)
local p = new_reader(0.37)
check("registers with the reader menu", log.registered == p)
local items = {}
p:addToMainMenu(items)
local entry = items.librarian_sync
check("lives in the Tools tab, not as 'NEW:'", entry and entry.sorting_hint == "tools")
check("offers five actions", entry and #entry.sub_item_table == 5)
check("with no map yet, the offline item says it needs Wi-Fi once",
      entry.sub_item_table[1].text_func() == "Your audiobook position (needs Wi-Fi once)")
check("before asking, the online label is a question", entry.sub_item_table[3].text_func() == "Where is my audiobook? (Wi-Fi)")
check("the jump is disabled until there is a position", entry.sub_item_table[4].enabled_func() == false)

-- ---- the audiobook position ----------------------------------------------------
http_reply = { code = 200, body = '{"document":"abc123","progress":"/body/DocFragment[9]/body/p[10]","percentage":0.42,"device":"Audiobookshelf"}' }
p:refreshAudio(false)
check("an audiobook position is remembered", p.audio and math.abs(p.audio.percentage - 0.42) < 1e-9)
check("it asks the gateway with the book's KOReader digest", log.requests[#log.requests] == "http://10.0.0.9:3000/librarian/audio/abc123")
check("every request is bounded by a short timeout",
      log.timeouts[1] and log.timeouts[1][1] == 5 and log.timeouts[1][2] == 8 and log.timeouts[2] == "reset")
check("it sends the Progress sync login", log.last_headers and log.last_headers["x-auth-user"] == "reader"
      and log.last_headers["x-auth-key"] == "k3y")
check("a background check says nothing", #log.shown == 0)
check("the label now shows the position", entry.sub_item_table[3].text_func() == "Audiobook on the server: 42%")
check("and the jump is enabled", entry.sub_item_table[4].enabled_func() == true)

p:showPositions()
check("both positions are shown when asked", (log.shown[#log.shown]:find("Audiobook: 42%%") ~= nil) and (log.shown[#log.shown]:find("Reading: 37%%") ~= nil))
check("...with which way the audiobook lies", (log.shown[#log.shown]:find("ahead of you") ~= nil))

local q = new_reader(0.42); q.audio = { percentage = 0.425 }; q:showPositions()
check("within a percent reads as in step", (log.shown[#log.shown]:find("in step") ~= nil))
local r = new_reader(0.60); r.audio = { percentage = 0.42 }; r:showPositions()
check("behind reads as behind", (log.shown[#log.shown]:find("behind you") ~= nil))

-- ---- moving the page only when asked ------------------------------------------
local before = #log.events
p:refreshAudio(false)
check("refreshing never moves the page", #log.events == before)
p:goToAudio()
local last = log.events[#log.events]
check("going there puts the current page on the back-stack first", log.stack == 1)
check("...then jumps to the audiobook's xpointer", last and last.name == "GotoXPointer" and last.arg == "/body/DocFragment[9]/body/p[10]")
entry.sub_item_table[5].callback()
check("'hand my place over' is KOSync's own push", log.events[#log.events].name == "KOSyncPushProgress")

-- ---- everything that is not an audiobook position ------------------------------
http_reply = { code = 200, body = '{"document":"abc123","progress":"/body/p[3]","percentage":0.1,"device":"Voxie"}' }
p:refreshAudio(false)
check("a replayed READING position is not mistaken for audio", p.audio == nil)

http_reply = { code = 404, body = '{"message":"No progress"}' }
local n = #log.shown
p:refreshAudio(true)
check("404 when asked explains there is none yet", #log.shown == n + 1 and (log.shown[#log.shown]:find("no audiobook position") ~= nil))

http_reply = { code = 200, body = "<html>proxy error</html>" }
p:refreshAudio(false)
check("garbage from the network is not trusted", p.audio == nil)

http_reply = { err = "timeout" }
local code, why = nil, nil
p:refreshAudio(true)
check("a timeout is reported, not raised", (log.shown[#log.shown]:find("did not answer") ~= nil))

online = false
p:refreshAudio(true)
check("offline, it says so and does not try", (log.shown[#log.shown]:find("Wi%-Fi") ~= nil))
online = true

-- ---- lifecycle -----------------------------------------------------------------
local reqs = #log.requests
local s = new_reader(0.1)
s.ui.document.file = "/nonexistent/book.epub"   -- io.popen of unzip will find nothing
local popened = false
local real_popen = io.popen
io.popen = function(...) popened = true; return real_popen(...) end
s:onReaderReady()
io.popen = real_popen
check("opening a book schedules one quiet check", log.scheduled[#log.scheduled].secs == 10)
check("...and sends nothing: pairing is by fingerprint", log.requests[reqs + 1] == nil)
check("...and runs no shell command (no unzip)", popened == false)

local nologin = new_reader(0.1)
nologin.ui.kosync = nil
local n2 = #log.shown
nologin:refreshAudio(true)
check("without a Progress sync login it says so and asks nothing",
      #log.shown == n2 + 1 and (log.shown[#log.shown]:find("Progress sync") ~= nil))
http_reply = { code = 401, body = "{}" }
p:refreshAudio(true)
check("a refused login is named as such", (log.shown[#log.shown]:find("did not accept") ~= nil))
local task = s.audio_task
s:onCloseWidget()
check("closing the book cancels the pending check", log.unscheduled[#log.unscheduled] == task and s.audio_task == nil)

check("plain HTTP asks LuaSec for nothing", log.last_req and log.last_req.verify == nil
      and log.last_req.cafile == nil)

-- ---- HTTPS (plugin 1.5) ----------------------------------------------------------
local function load_with(url)
    local c = assert(loadstring((template:gsub("__GATEWAY_URL__", url)), "main.lua"))
    local P = c()
    local ui = new_reader(0.2).ui
    local r = setmetatable({ ui = ui }, { __index = P })
    r:init()
    return r
end
local secure = load_with("https://voxie.example.ts.net/")
http_reply = { code = 200, body = '{"document":"abc123","progress":"/body/p[1]","percentage":0.5,"device":"Audiobookshelf"}' }
os.remove("data/ca-bundle.crt")
secure:refreshAudio(false)
check("an https:// gateway is asked over https, trailing slash trimmed",
      log.requests[#log.requests] == "https://voxie.example.ts.net/librarian/audio/abc123", log.requests[#log.requests])
check("without KOReader's CA bundle it still works, unchecked", log.last_req.verify == nil)
-- data/ is KOReader's folder on a device; in a fresh clone it does not exist.
local had_data = os.rename("data", "data")
if not had_data then os.execute("mkdir data") end
local ca = assert(io.open("data/ca-bundle.crt", "w")); ca:write("x"); ca:close()
secure:refreshAudio(false)
os.remove("data/ca-bundle.crt")
if not had_data then os.remove("data") end
check("with the bundle, the certificate is checked against it",
      log.last_req.verify == "peer" and log.last_req.cafile == "data/ca-bundle.crt")

-- ---- the map kept on the device (plugin 1.6) ---------------------------------------
-- The gateway's own packing of the shared examples' map (core/landing.py).
local h = io.popen([==[python3 -c "
import sys, json; sys.path.insert(0, '.')
from core import landing
ex = json.load(open('tools/landing_examples.json'))
n = [('point(/1' + a + ')', b, c) for a, b, c in ex['map']['nodes']]
p, d, l = landing.pack_map(landing.build_map(n, [tuple(r) for r in ex['chunk_rows']]))
sys.stdout.write(d + ' ' + str(l) + '\n'); sys.stdout.flush(); sys.stdout.buffer.write(p)"]==])
local packed_all = h:read("*a"); h:close()
local MAP_SHA, MAP_LEN, PACKED = packed_all:match("^(%x+) (%d+)\n(.*)$")
check("the gateway's packed map is at hand for these checks", MAP_SHA ~= nil and #PACKED > 0)
local MAP_FILE = DATA .. "/.voxie_maps/abc123.map"
local function map_reply(sha) return { code = 200, body = PACKED,
    headers = { ["x-voxie-map-sha256"] = sha or MAP_SHA, ["x-voxie-map-length"] = MAP_LEN } } end

local m = new_reader(0.4)
local mitems = {}; m:addToMainMenu(mitems)
local offline_item, listened_item = mitems.librarian_sync.sub_item_table[1], mitems.librarian_sync.sub_item_table[2]

http_reply = map_reply("0000")
check("a download whose checksum does not match is refused", m:refreshMap() == false and m.map == nil
      and io.open(MAP_FILE) == nil)
http_reply = map_reply()
check("the map is downloaded and loaded", m:refreshMap() == true and m.map ~= nil and #m.map.nodes == 10)
check("...from the book's own map address", log.requests[#log.requests] == "http://10.0.0.9:3000/librarian/map/abc123")
check("...into KOReader's folder, not beside the books", io.open(MAP_FILE) ~= nil)
http_reply = { code = 304, body = "" }
check("a map still current is not downloaded again", m:refreshMap() == true
      and log.requests[#log.requests] == "http://10.0.0.9:3000/librarian/map/abc123?have=" .. MAP_SHA)

online = false
local sent_before = #log.requests
log.xpointer = "/body/DocFragment[3]/body/p[21]/span[1]/text().0"
check("offline, this page's audiobook position comes from the map",
      offline_item.text_func() == "Your audiobook position: chapter 2, 0:30", offline_item.text_func())
offline_item.callback()
check("...and its details name the chapter and the whole-book time",
      log.shown[#log.shown]:find("chapter 2 %(Two%) at 0:30") and log.shown[#log.shown]:find("2:10 from the start"),
      log.shown[#log.shown])
log.xpointer = "/body/DocFragment[3]/body/p[99]"
offline_item.callback()
check("a page the map does not know is labelled an estimate", log.shown[#log.shown]:find("estimate") ~= nil)

-- I listened up to...
local stack_before = log.stack
listened_item.callback()
local d = dialogs[#dialogs]
check("'I listened up to' asks for a chapter (this page's, to start with) and a time",
      d and #d.fields == 2 and d.fields[1].text == "2",
      d and d.fields[1].text)
local go = d.buttons[1][2].callback
d.answers = { "2", "0:01" }; go()
local ev = log.events[#log.events]
check("chapter 2 at 0:01 lands on that chapter's first paragraph, never the one before",
      ev.name == "GotoXPointer" and ev.arg == "/body/DocFragment[3]/body/p[20]", ev.arg)
check("...with the reader's own page on the back-stack", log.stack == stack_before + 1)
d.answers = { "1", "0:51" }; go()
check("1 s into a paragraph lands on the one before (3 s of context)",
      log.events[#log.events].arg == "/body/DocFragment[3]/body/p[2]", log.events[#log.events].arg)
local evs = #log.events
d.answers = { "9", "1:00" }; go()
check("a chapter that does not exist is refused", #log.events == evs and log.shown[#log.shown]:find("from 1 to 4"))
d.answers = { "2", "half past" }; go()
check("a time that is not a time is refused", #log.events == evs and log.shown[#log.shown]:find("14:32"))
d.answers = { "2", "5:00" }; go()
check("a time past the chapter's end is refused", #log.events == evs and log.shown[#log.shown]:find("only 1:40 long"))
check("none of it touched the network", #log.requests == sent_before)

-- A damaged or changed map is never used.
local f = assert(io.open(MAP_FILE, "rb")); local stored = f:read("*a"); f:close()
f = assert(io.open(MAP_FILE, "wb")); f:write(stored:sub(1, -2) .. string.char((stored:byte(-1) + 1) % 256)); f:close()
check("a damaged map fails its checksum and is removed", m:loadMap() == nil and io.open(MAP_FILE) == nil)
check("...and the item says it needs Wi-Fi again", offline_item.text_func() == "Your audiobook position (needs Wi-Fi once)")
online = true
http_reply = map_reply()
m:refreshMap()
http_reply = { code = 404, body = "{}" }
m:refreshMap()
check("a book whose audiobook is gone forgets its map", m.map == nil and io.open(MAP_FILE) == nil
      and offline_item.text_func() == "This book has no audiobook")

-- Back online: the page read offline is sent first, with when it was read.
local r2 = new_reader(0.55)
r2.ui.kosync.device_id = "dev1"
online = false
log.xpointer = "/body/DocFragment[3]/body/p[21]/span[3]/text().7"
r2:onPageUpdate(12)
check("a page turned offline is remembered with its time", type(r2.read_offline_at) == "number")
local read_when = r2.read_offline_at
queue.items = { { document = "abc123", progress = "/old" }, { document = "other", progress = "/x" } }
online = true
http_reply = { code = 200, body = '{"document":"abc123"}' }
r2:onNetworkConnected()
local task2 = log.scheduled[#log.scheduled]
check("on reconnect, it runs before KOSync's own pull (0.5 s)", task2.secs < 0.5)
task2.fn()
local put
for i = #log.requests, 1, -1 do if log.methods[i] == "PUT" then put = i break end end
local body = put and log.bodies[put] or ""
check("...and pushes the page it shows to the progress endpoint",
      put and log.requests[put] == "http://10.0.0.9:3000/syncs/progress"
      and body:find('"progress":"/body/DocFragment%[3%]/body/p%[21%]/span%[3%]/text%(%).7"'), body)
check("...with when it was read", body:find('"read_at":' .. read_when) ~= nil
      and body:find('"document":"abc123"') ~= nil, body)
check("...then forgets it", r2.read_offline_at == nil)
check("KOSync's older queued page for this book is dropped, other books' kept",
      #queue.items == 1 and queue.items[1].document == "other")
local puts = 0
for i = 1, #log.requests do if log.methods[i] == "PUT" then puts = puts + 1 end end
r2:onNetworkConnected(); log.scheduled[#log.scheduled].fn()
local puts_after = 0
for i = 1, #log.requests do if log.methods[i] == "PUT" then puts_after = puts_after + 1 end end
check("a reconnect with nothing read offline pushes nothing", puts_after == puts)
r2:onPageUpdate(13)
check("a page turned online is left to KOSync", r2.read_offline_at == nil)
os.execute("rm -rf '" .. DATA .. "'")

print("\n" .. string.rep("=", 60))
print(string.format("%d checks: %d passed, %d FAILED", passed + failed, passed, failed))
os.exit(failed == 0 and 0 or 1)
