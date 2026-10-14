--[[ Voxie: progress sync never stops you reading.

KOReader's progress sync (KOSync), with auto sync on and Wi-Fi set to "turn
on", pulls the other device's page every time a book opens and every time the
Kobo wakes, and pushes it every time the Kobo sleeps. Offline, each of those
runs KOReader's interactive connect: "Scanning for networks…" in a box that
holds the screen until a network answers or the scan gives up, up to two
minutes away from home (seen on a Clara 2E, 2026-10-14).

This patch keeps every automatic sync and takes the wait out of it:

  - an automatic pull while offline starts Wi-Fi in the background (the same
    restore-wifi-async.sh KOReader uses to restore Wi-Fi at boot) and returns;
    when the network comes up, KOSync's own NetworkConnected handler pushes
    what was queued and then pulls, and Voxie's plugin hands over the page
    read offline. Away from every known network the attempt ends silently.
  - an automatic push while offline (the one at sleep) is queued, exactly as
    KOSync already queues the push when a book closes offline.
  - anything you ask for yourself (Push/Pull progress in the menu) is
    interactive and behaves as before.

Installed by `voxie setup-kobo` into .adds/koreader/patches/. Delete the file
to go back to KOReader's own behaviour.
]]

local Device = require("device")
local NetworkMgr = require("ui/network/manager")
local logger = require("logger")
local userpatch = require("userpatch")

-- Start Wi-Fi without any UI. True when a connection is up or on its way.
local function connectInBackground()
    if NetworkMgr:isConnected() then return true end
    if NetworkMgr.pending_connection or NetworkMgr.pending_connectivity_check then return true end
    if not Device:hasWifiRestore() then return false end
    logger.info("Voxie: offline; restoring Wi-Fi in the background")
    NetworkMgr:restoreWifiAsync()
    -- Polls quietly (no widget), broadcasts NetworkConnected on success and
    -- turns the radio back off if no known network answers.
    NetworkMgr:scheduleConnectivityCheck()
    return true
end

-- KOSync's own offline queue, as its onCloseDocument fills it.
local function queuePush(kosync)
    local ok, KOSyncQueue = pcall(require, "KOSyncQueue")
    local digest = ok and kosync.getDocumentDigest and kosync:getDocumentDigest()
    if not digest then return end
    KOSyncQueue:push({
        document = digest,
        metadata = kosync.getMetadata and kosync:getMetadata() or nil,
        progress = kosync:getLastProgress(),
        percentage = kosync:getLastPercent(),
        device = kosync.settings.kosync_hostname or Device.model,
        device_id = kosync.device_id,
    })
end

userpatch.registerPatchPluginFunc("kosync", function(KOSync)
    -- Plugins are dofile()d afresh for each reader, but never wrap twice.
    if KOSync._voxie_background_wifi then return end
    KOSync._voxie_background_wifi = true

    local getProgress = KOSync.getProgress
    KOSync.getProgress = function(self, ensure_networking, interactive)
        if ensure_networking and not interactive and not NetworkMgr:isOnline() then
            connectInBackground()
            return
        end
        return getProgress(self, ensure_networking, interactive)
    end

    local updateProgress = KOSync.updateProgress
    KOSync.updateProgress = function(self, ensure_networking, interactive, on_suspend)
        if ensure_networking and not interactive and not NetworkMgr:isOnline() then
            queuePush(self)
            return
        end
        return updateProgress(self, ensure_networking, interactive, on_suspend)
    end
end)
