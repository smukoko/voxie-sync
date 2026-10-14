local DataStorage = require("datastorage")
local Event = require("ui/event")
local InfoMessage = require("ui/widget/infomessage")
local JSON = require("json")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local http = require("socket.http")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local ltn12 = require("ltn12")
local sha2 = require("ffi/sha2")
local socket = require("socket")
local socketutil = require("socketutil")
local zlib = require("ffi/zlib")
local Landing = require("voxie_landing")
local T = require("ffi/util").template
local _ = require("gettext")

-- The gateway's base URL, substituted at install time by
-- libra_cli/main.py:setup_kobo: "http://91.99.12.34:3000", or an HTTPS
-- address such as "https://voxie.your-tailnet.ts.net". This file is the
-- single source of truth for the plugin -- do not hand-edit and copy elsewhere.
local GATEWAY = ("__GATEWAY_URL__"):gsub("/+$", "")

-- KOReader's LuaSocket hands https:// to LuaSec, which checks no certificate
-- by default ("verify none"): encrypted, but anyone in the middle could pose
-- as the gateway. KOReader ships Mozilla's CA bundle, and starts in its own
-- folder, so this relative path is the bundle on every device.
local CA_BUNDLE = "data/ca-bundle.crt"

-- LuaSocket's default block timeout is 60 seconds, and every call here runs
-- on the UI thread. On wifi without internet -- a hotel captive portal, a
-- router with its uplink down -- opening a book used to freeze the reader for
-- a full minute on the sidecar ping. Nothing this plugin asks for is worth
-- more than a few seconds of a reader's patience.
local BLOCK_TIMEOUT = 5
local TOTAL_TIMEOUT = 8

-- The gateway answers a progress pull with device = "Audiobookshelf" when the
-- position it returns was translated from the audiobook, and "Voxie" when
-- it is only replaying the reader's own stored position. Only the first is an
-- audiobook position.
local AUDIO_DEVICE = "Audiobookshelf"

-- Long enough for KOSync's own pull on open to finish -- and to have jumped,
-- if the audiobook was ahead -- so the position shown here is the one the
-- reader is actually looking at afterwards.
local AUDIO_CHECK_DELAY = 10

-- Each book's map (positions only, ~55 KB for a 16-hour novel) is kept here,
-- so the landing rules work with no network. Inside KOReader's own folder
-- (.adds/koreader on a Kobo), never beside the books, where Nickel would see
-- it. One file per book: a header line with the sha256 and the inflated
-- length, then the gateway's zlib bytes exactly as they arrived.
local MAP_DIR = DataStorage:getDataDir() .. "/.voxie_maps"
local MAP_MAGIC = "VOXIEMAP 1"

-- Before KOSync's own reconnect pull (it waits 0.5 s), so the gateway already
-- holds the page read offline when that pull decides who is ahead.
local RECONNECT_PUSH_DELAY = 0.1

local LibrarianSync = WidgetContainer:extend{
    name = "LibrarianSync",
    is_doc_only = true,
    description = "Pairs books with their audiobooks and shows where the audiobook is.",
    version = "1.6",
}

-- code, body, headers; or nil, error.
local function request(url, headers, method, body)
    local sink = {}
    local req = { url = url, method = method or "GET", headers = headers, sink = ltn12.sink.table(sink) }
    if body then
        req.source = ltn12.source.string(body)
        headers["content-type"] = "application/json"
        headers["content-length"] = tostring(#body)
    end
    if url:match("^https://") then
        local bundle = io.open(CA_BUNDLE, "r")
        if bundle then
            bundle:close()
            req.verify, req.cafile = "peer", CA_BUNDLE
        else
            logger.warn("LibrarianSync: no", CA_BUNDLE, "- the gateway's certificate is not checked")
        end
    end
    socketutil:set_timeout(BLOCK_TIMEOUT, TOTAL_TIMEOUT)
    local ok, code, resp_headers = pcall(function()
        local _, c, h = http.request(req)
        return c, h
    end)
    socketutil:reset_timeout()
    if not ok or type(code) ~= "number" then
        return nil, tostring(code)
    end
    return code, table.concat(sink), resp_headers or {}
end

local function percent(x)
    return string.format("%d%%", math.floor((x or 0) * 100 + 0.5))
end

local function read_file(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read("*a")
    f:close()
    return data
end

-- rapidjson where KOReader has it (fast on a 300 KB map), else KOReader's json.
local function decode_json(text)
    local ok, rapidjson = pcall(require, "rapidjson")
    local decoder = ok and rapidjson or JSON
    local decoded_ok, data = pcall(decoder.decode, text)
    if decoded_ok then return data end
end

function LibrarianSync:init()
    -- {percentage=, progress=} once the gateway has given an audiobook
    -- position for this book; nil when it has none or has not been asked.
    self.audio = nil
    -- The book's map, verified and decoded: {sha=, nodes=, chapters=}.
    self.map = nil
    -- os.time() of the last page turned while offline, until it is pushed.
    self.read_offline_at = nil
    self.ui.menu:registerToMainMenu(self)
end

function LibrarianSync:say(text, timeout)
    UIManager:show(InfoMessage:new{ text = text, timeout = timeout or 3 })
end

function LibrarianSync:digest()
    return self.ui.doc_settings and self.ui.doc_settings:readSetting("partial_md5_checksum")
end

function LibrarianSync:onReaderReady()
    -- The gateway pairs a book with its map by the file's own KOReader hash,
    -- computed when the map arrives, so there is nothing to announce here.
    -- Versions up to 1.3 pinged the book's watermark (read with `unzip`, which
    -- not every KOReader device has), and the gateway married that ping to the
    -- next request from any device.
    self:loadMap()
    -- Quietly, and never by turning wifi on: KOSync owns the network nagging.
    self.audio_task = function()
        self:refreshMap()
        self:refreshAudio(false)
    end
    UIManager:scheduleIn(AUDIO_CHECK_DELAY, self.audio_task)
end

-- The reader's progress-sync login, which the gateway requires on every call.
-- Borrowed from KOSync rather than stored twice, so a new password set in
-- Tools > Progress sync is used here too.
function LibrarianSync:authHeaders()
    local kosync = self.ui and self.ui.kosync
    local settings = kosync and kosync.settings or {}
    if not settings.username or not settings.userkey then return nil end
    return { ["x-auth-user"] = settings.username, ["x-auth-key"] = settings.userkey,
             ["accept"] = "application/vnd.koreader.v1+json" }
end

function LibrarianSync:onCloseWidget()
    if self.audio_task then
        UIManager:unschedule(self.audio_task)
        self.audio_task = nil
    end
end

function LibrarianSync:readingPercent()
    if self.ui.document.info.has_pages then
        return self.ui.paging:getLastPercent()
    end
    return self.ui.rolling:getLastPercent()
end

-- --- The map kept on the device ---------------------------------------------

function LibrarianSync:mapPath(digest)
    return MAP_DIR .. "/" .. digest .. ".map"
end

-- The stored map, checked against its own sha256 every time it is read: a
-- changed or damaged map is never used. It is deleted, and the next time the
-- device is online it is downloaded again.
function LibrarianSync:loadMap()
    self.map = nil
    local digest = self:digest()
    if not digest then return nil end
    local path = self:mapPath(digest)
    local data = read_file(path)
    if not data then return nil end
    local sha, length, packed = data:match("^" .. MAP_MAGIC .. " (%x+) (%d+)\n(.*)$")
    local book_map
    if sha and sha2.sha256(packed) == sha then
        local ok, raw = pcall(zlib.zlib_uncompress, packed, tonumber(length))
        book_map = ok and decode_json(raw)
    end
    if type(book_map) ~= "table" or type(book_map.nodes) ~= "table" or type(book_map.chapters) ~= "table" then
        logger.warn("LibrarianSync: the stored map for", digest, "is damaged; removed")
        os.remove(path)
        return nil
    end
    self.map = { sha = sha, nodes = book_map.nodes, chapters = book_map.chapters }
    return self.map
end

-- Fetch the map when the gateway has a newer one. Online only, never asks for
-- Wi-Fi. Returns true when a usable map is loaded afterwards.
function LibrarianSync:refreshMap()
    if not self.ui or not self.ui.document or not NetworkMgr:isOnline() then return self.map ~= nil end
    local digest, headers = self:digest(), self:authHeaders()
    if not digest or not headers then return self.map ~= nil end
    local url = GATEWAY .. "/librarian/map/" .. digest
    if self.map then url = url .. "?have=" .. self.map.sha end
    local code, body, resp = request(url, headers)
    if code == 304 then return true end
    if code == 404 then
        -- No audiobook for this book (any more): forget a stale copy.
        os.remove(self:mapPath(digest))
        self.map = nil
        self.map_missing = true
        return false
    end
    local sha = resp and (resp["x-voxie-map-sha256"] or resp["X-Voxie-Map-Sha256"])
    local length = resp and (resp["x-voxie-map-length"] or resp["X-Voxie-Map-Length"])
    if code ~= 200 or not sha or not tonumber(length) or sha2.sha256(body) ~= sha then
        logger.warn("LibrarianSync: map download failed:", code)
        return self.map ~= nil
    end
    if lfs.attributes(MAP_DIR, "mode") ~= "directory" then lfs.mkdir(MAP_DIR) end
    local tmp = self:mapPath(digest) .. ".part"
    local f = io.open(tmp, "wb")
    if not f then return self.map ~= nil end
    f:write(MAP_MAGIC, " ", sha, " ", length, "\n", body)
    f:close()
    os.remove(self:mapPath(digest))
    os.rename(tmp, self:mapPath(digest))
    self.map_missing = nil
    return self:loadMap() ~= nil
end

-- --- Offline: the landing rules on the stored map -----------------------------

-- Chapter, time in it, whole-book time, and whether the page was in the map.
function LibrarianSync:pageInAudio()
    if not self.map or self.ui.document.info.has_pages then return nil end
    local seconds, from_map = Landing.audio_time(self.map.nodes, self.map.chapters,
                                                 self.ui.rolling:getLastProgress(), self:readingPercent())
    local chapter, offset = Landing.chapter_at(self.map.chapters, seconds)
    return chapter, offset, seconds, from_map
end

function LibrarianSync:positionText()
    local chapter, offset = self:pageInAudio()
    if not chapter then
        if self.map_missing then return _("This book has no audiobook") end
        return _("Your audiobook position (needs Wi-Fi once)")
    end
    return T(_("Your audiobook position: chapter %1, %2"), chapter, Landing.format_time(offset))
end

function LibrarianSync:showPageInAudio()
    if not self.map and not self:refreshMap() then
        if self.map_missing then
            self:say(_("This book has no audiobook on your Voxie server."))
        else
            self:say(_("Connect to Wi-Fi once, with this book open, so Voxie can keep its audiobook map on your Kobo."), 5)
        end
        return
    end
    local chapter, offset, seconds, from_map = self:pageInAudio()
    local title = self.map.chapters[chapter] and self.map.chapters[chapter][1] or ""
    local text = T(_("To listen from this page, go to chapter %1 (%2) at %3 in your audiobook.\n\nThat is %4 from the start."),
                   chapter, title, Landing.format_time(offset), Landing.format_time(seconds))
    if not from_map then
        text = text .. "\n\n" .. _("This page is not in the map, so this is an estimate from how far you are in the book.")
    end
    self:say(text, 8)
end

function LibrarianSync:jumpTo(position, message)
    -- Onto the back-stack first, so Back returns the reader to their own page:
    -- a jump the reader did not mean to keep should cost one tap, not a search.
    if self.ui.link then
        self.ui.link:addCurrentLocationToStack()
    end
    if self.ui.document.info.has_pages then
        self.ui:handleEvent(Event:new("GotoPage", tonumber(position)))
    else
        self.ui:handleEvent(Event:new("GotoXPointer", position))
    end
    self:say(message, 4)
end

-- "I listened up to...": chapter and time, as the audiobook player shows them,
-- to the page. Works with no network.
function LibrarianSync:askListenedTo()
    if not self.map and not self:refreshMap() then
        self:showPageInAudio()   -- says why there is no map
        return
    end
    local chapters = self.map.chapters
    local current = self:pageInAudio() or 1
    local dialog
    dialog = MultiInputDialog:new{
        title = _("I listened up to…"),
        fields = {
            { description = T(_("Chapter (1 to %1)"), #chapters), text = tostring(current),
              input_type = "number", hint = "1" },
            { description = _("Time in that chapter"), text = "", hint = "14:32" },
        },
        buttons = { {
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Go to that page"), is_enter_default = true, callback = function()
                local fields = dialog:getFields()
                local chapter, offset = tonumber(fields[1]), Landing.parse_time(fields[2])
                if not chapter or chapter < 1 or chapter > #chapters or chapter % 1 ~= 0 then
                    self:say(T(_("Choose a chapter from 1 to %1."), #chapters))
                    return
                end
                if not offset then
                    self:say(_("Type the time as the player shows it, for example 14:32 or 1:02:03."))
                    return
                end
                local ch = chapters[chapter]
                if offset > ch[3] - ch[2] + 1 then
                    self:say(T(_("Chapter %1 is only %2 long."), chapter, Landing.format_time(ch[3] - ch[2])))
                    return
                end
                UIManager:close(dialog)
                local position = Landing.landing_position(self.map.nodes, chapters, ch[2] + offset)
                if position then
                    self:jumpTo(position, T(_("Moved to chapter %1, %2. Use Back to return to where you were."),
                                            chapter, Landing.format_time(offset)))
                end
            end },
        } },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

-- --- Back online ----------------------------------------------------------------

function LibrarianSync:onPageUpdate()
    -- The state KOReader's network listener keeps, not isOnline(): that one
    -- looks a hostname up, and offline each page turn would wait on it.
    if not NetworkMgr:getConnectionState() then
        self.read_offline_at = os.time()
    end
end

-- Pages read offline reach the gateway the moment Wi-Fi returns, with the
-- time they were read: the gateway then knows whether the reading or the
-- listening is newer (a phone that listened offline reports its own time).
-- KOSync's queue holds an older page for this book; drained after this, it
-- would write that older place into the audiobook, so this push replaces it.
function LibrarianSync:onNetworkConnected()
    UIManager:scheduleIn(RECONNECT_PUSH_DELAY, function()
        if self.read_offline_at then self:pushOfflineReading() end
        self:refreshMap()
    end)
end

function LibrarianSync:pushOfflineReading()
    local digest, headers = self:digest(), self:authHeaders()
    if not self.ui or not self.ui.document or self.ui.document.info.has_pages or not digest or not headers then
        return
    end
    local kosync = self.ui.kosync
    local body = JSON.encode({
        document = digest,
        progress = self.ui.rolling:getLastProgress(),
        percentage = self:readingPercent(),
        device = "Voxie",
        device_id = kosync and kosync.device_id or "voxie",
        read_at = self.read_offline_at,
    })
    local code = request(GATEWAY .. "/syncs/progress", headers, "PUT", body)
    if code ~= 200 then return end
    self.read_offline_at = nil
    local ok, queue = pcall(require, "KOSyncQueue")
    if ok and type(queue) == "table" and queue.load and queue.save then
        local kept = {}
        for _, item in ipairs(queue:load()) do
            if item.document ~= digest then table.insert(kept, item) end
        end
        queue:save(kept)
    end
end

-- --- Online: where the audiobook is now -----------------------------------------

-- Ask the gateway where the audiobook is. This reads the same endpoint KOSync
-- pulls from, and deliberately decides nothing: whether to MOVE the page stays
-- KOSync's call, made on its own forward/backward rules. This only remembers
-- the answer, so the menu can show it and offer to go there.
function LibrarianSync:refreshAudio(interactive)
    if not self.ui or not self.ui.document then return end
    if not NetworkMgr:isOnline() then
        if interactive then
            self:say(_("Connect to Wi-Fi to check where your audiobook is. Without Wi-Fi, use \"I listened up to…\"."), 5)
        end
        return
    end
    local digest = self:digest()
    if not digest then
        if interactive then self:say(_("This book has not been fingerprinted yet. Try again in a moment.")) end
        return
    end

    local headers = self:authHeaders()
    if not headers then
        if interactive then self:say(_("Log in under Tools > Progress sync first.")) end
        return
    end
    -- Not /syncs/progress: that answers KOSync, where your own place wins
    -- when it is newer, so it cannot also tell you where the audio is.
    local code, body = request(GATEWAY .. "/librarian/audio/" .. digest, headers)
    if code ~= 200 then
        self.audio = nil
        if interactive then
            if code == 404 then
                self:say(_("There is no audiobook position for this book yet."))
            elseif code == 401 then
                self:say(_("The gateway did not accept your Progress sync login."))
            else
                self:say(_("The Voxie gateway did not answer."))
            end
        end
        return
    end

    local ok, data = pcall(JSON.decode, body)
    if not ok or type(data) ~= "table" or data.device ~= AUDIO_DEVICE or not tonumber(data.percentage) then
        self.audio = nil
        if interactive then self:say(_("There is no audiobook position for this book yet.")) end
        return
    end

    self.audio = { percentage = tonumber(data.percentage), progress = data.progress }
    if interactive then self:showPositions() end
end

function LibrarianSync:showPositions()
    local audio, reading = self.audio.percentage, self:readingPercent()
    local delta = audio - reading
    local where
    if math.abs(delta) < 0.01 then
        where = _("You are in step with your audiobook.")
    elseif delta > 0 then
        where = _("Your audiobook is ahead of you.")
    else
        where = _("Your audiobook is behind you.")
    end
    self:say(T(_("Audiobook: %1\nReading: %2\n\n%3"), percent(audio), percent(reading), where), 6)
end

function LibrarianSync:goToAudio()
    if not self.audio or not self.audio.progress then return end
    self:jumpTo(self.audio.progress, _("Moved to your audiobook position. Use Back to return to where you were."))
end

function LibrarianSync:addToMainMenu(menu_items)
    menu_items.librarian_sync = {
        text = _("Voxie"),
        -- Beside KOSync's "Progress sync" in the Tools tab. Without a hint the
        -- menu sorter files an unknown item in the first tab as "NEW: ...".
        sorting_hint = "tools",
        sub_item_table = {
            -- The first two work with no network, from the map kept on the Kobo.
            {
                text_func = function() return self:positionText() end,
                keep_menu_open = true,
                callback = function() self:showPageInAudio() end,
            },
            {
                text = _("I listened up to…"),
                separator = true,
                callback = function() self:askListenedTo() end,
            },
            {
                text_func = function()
                    if self.audio then
                        return T(_("Audiobook on the server: %1"), percent(self.audio.percentage))
                    end
                    return _("Where is my audiobook? (Wi-Fi)")
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:refreshAudio(true)
                    if touchmenu_instance then touchmenu_instance:updateItems() end
                end,
            },
            {
                text = _("Go to where the audiobook is"),
                enabled_func = function() return self.audio ~= nil and self.audio.progress ~= nil end,
                callback = function() self:goToAudio() end,
            },
            {
                text = _("Hand my place to the audiobook now"),
                -- KOSync's own push, interactive, so it reports success or failure itself.
                callback = function() self.ui:handleEvent(Event:new("KOSyncPushProgress")) end,
            },
        },
    }
end

return LibrarianSync
