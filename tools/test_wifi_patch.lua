-- Exercise koreader_patches/2-wifi-in-background.lua under LuaJIT, off the device.
--
-- The patch wraps KOSync's getProgress/updateProgress through KOReader's
-- userpatch hook. These stubs record what the wrapped calls do, to prove the
-- one rule it adds: an automatic sync while offline never reaches KOReader's
-- blocking connect (the original functions, which call willRerunWhenOnline).
--
--     luajit tools/test_wifi_patch.lua

local passed, failed = 0, 0
local function check(name, ok, detail)
    if ok then passed = passed + 1 else failed = failed + 1 end
    print((ok and "✅ " or "❌ ") .. name .. (detail and ("  (" .. tostring(detail) .. ")") or ""))
end

local net = { online = false, connected = false, restores = 0, checks = 0 }
local NetworkMgr = {
    isOnline = function() return net.online end,
    isConnected = function() return net.connected end,
    restoreWifiAsync = function() net.restores = net.restores + 1 end,
    scheduleConnectivityCheck = function(self) net.checks = net.checks + 1; self.pending_connectivity_check = true end,
}
local device = { wifi_restore = true, model = "Kobo_test" }
local queued = {}
local registered = {}

local function stub(name, value) package.preload[name] = function() return value end end
stub("device", { hasWifiRestore = function() return device.wifi_restore end, model = device.model })
stub("ui/network/manager", NetworkMgr)
stub("logger", { info = function() end, dbg = function() end, warn = function() end })
stub("userpatch", { registerPatchPluginFunc = function(name, fn) registered[name] = fn end })
stub("KOSyncQueue", { push = function(_, item) table.insert(queued, item) end })

dofile("koreader_patches/2-wifi-in-background.lua")
check("registers a patch for the kosync plugin", type(registered.kosync) == "function")

-- A KOSync class as the plugin loader hands it over, with recording originals.
local calls = {}
local function newKOSync()
    local K = {
        settings = { username = "u", userkey = "k" },
        device_id = "dev",
    }
    function K:getProgress(ensure, interactive) table.insert(calls, { "get", ensure, interactive }) end
    function K:updateProgress(ensure, interactive, on_suspend) table.insert(calls, { "update", ensure, interactive, on_suspend }) end
    function K:getDocumentDigest() return "digest1" end
    function K:getMetadata() return { title = "t" } end
    function K:getLastProgress() return "/body/DocFragment[3]" end
    function K:getLastPercent() return 0.42 end
    return K
end
local function reset()
    calls, queued = {}, {}
    net.restores, net.checks = 0, 0
    NetworkMgr.pending_connection, NetworkMgr.pending_connectivity_check = nil, nil
end

local K = newKOSync()
registered.kosync(K)
local wrapped_get = K.getProgress
registered.kosync(K)
check("wraps a class only once", K.getProgress == wrapped_get)

-- Opening a book or waking offline: no blocking connect, Wi-Fi in the background.
reset(); net.online, net.connected = false, false
K:getProgress(true, false)
check("offline automatic pull never reaches the blocking connect", #calls == 0, #calls)
check("offline automatic pull restores Wi-Fi in the background", net.restores == 1 and net.checks == 1,
      net.restores .. "/" .. net.checks)

reset(); NetworkMgr.pending_connectivity_check = true
K:getProgress(true, false)
check("a second pull while a connection is on its way starts nothing more", net.restores == 0 and #calls == 0)

reset(); net.connected = true
K:getProgress(true, false)
check("associated but not yet online: waits for the connection already up", net.restores == 0 and #calls == 0)
net.connected = false

reset(); device.wifi_restore = false
K:getProgress(true, false)
check("a device without background restore still never blocks", #calls == 0 and net.restores == 0)
device.wifi_restore = true

-- What the reader asks for themselves keeps KOReader's own behaviour.
reset()
K:getProgress(true, true)
check("a pull asked for in the menu is untouched", #calls == 1 and calls[1][3] == true)

reset(); net.online = true
K:getProgress(true, false)
check("online, an automatic pull runs as before", #calls == 1 and calls[1][1] == "get" and net.restores == 0)
K:getProgress(false, false)
check("a pull that never asked for the network is untouched", #calls == 2)

-- Going to sleep offline: queued, never a connect.
reset(); net.online = false
K:updateProgress(true, false, true)
check("offline push at sleep never reaches the blocking connect", #calls == 0)
check("offline push at sleep is queued for the next connection",
      #queued == 1 and queued[1].document == "digest1" and queued[1].percentage == 0.42
      and queued[1].progress == "/body/DocFragment[3]" and queued[1].device_id == "dev")
check("offline push does not start Wi-Fi as the device sleeps", net.restores == 0)

reset()
K:updateProgress(true, true)
check("a push asked for in the menu is untouched", #calls == 1 and #queued == 0)

reset(); net.online = true
K:updateProgress(true, false, true)
check("online, the push at sleep runs as before", #calls == 1 and #queued == 0)

reset(); net.online = false
local nodigest = newKOSync()
nodigest.getDocumentDigest = function() return nil end
registered.kosync(nodigest)
nodigest:updateProgress(true, false, true)
check("no document digest: nothing queued, nothing blocks", #queued == 0 and #calls == 0)

print(string.format("\n%d passed, %d failed", passed, failed))
os.exit(failed == 0 and 0 or 1)
