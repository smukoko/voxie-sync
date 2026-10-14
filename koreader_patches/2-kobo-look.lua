--[[ Voxie: the reading screen of a Kobo, inside KOReader.

Installed by `voxie setup-kobo --look kobo`, removed by `--look koreader`.
It draws what a Kobo shows while you read, and nothing else changes:

  - above the text, the chapter and where you are in it:
        CHAPTER 1 · 3 OF 24
  - below the text, the book and where you are in it, and a hairline of
    progress along the bottom edge:
        THE YELLOW WALLPAPER · 8 OF 68
  - a tap in the middle of the page opens a Kobo-style toolbar: Back to the
    home screen, brightness, type (KOReader's own Aa panel), the full
    KOReader menu, the page and a slider, the chapter list, and the
    audiobook (Voxie's "where is my audiobook").

My Books shows covers, titles and authors without file sizes, as long as you
have not chosen otherwise in KOReader's own cover menu. Nothing of that is
written anywhere: it is a default while this file is here.

KOReader's own footer is switched off by the installer while this look is on
(the header and footer here replace it), and taps on links and highlights,
the page turns at the sides and KOReader's menus at the top and bottom edges
all work as before. Only reflowable books (EPUB, KEPUB, FB2...) get the
header and footer; a PDF keeps KOReader's page.

Icons live in .adds/voxie/look/ on the device.
]]

local Blitbuffer = require("ffi/blitbuffer")
local Device = require("device")
local Event = require("ui/event")
local Font = require("ui/font")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local HorizontalGroup = require("ui/widget/horizontalgroup")
local HorizontalSpan = require("ui/widget/horizontalspan")
local ImageWidget = require("ui/widget/imagewidget")
local InputContainer = require("ui/widget/container/inputcontainer")
local LineWidget = require("ui/widget/linewidget")
local OverlapGroup = require("ui/widget/overlapgroup")
local ReaderUI = require("apps/reader/readerui")
local ReaderTypeset = require("apps/reader/modules/readertypeset")
local ReaderView = require("apps/reader/modules/readerview")
local Size = require("ui/size")
local TextWidget = require("ui/widget/textwidget")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local datetime = require("datetime")
local logger = require("logger")
local userpatch = require("userpatch")
local _ = require("gettext")
local T = require("ffi/util").template
local Screen = Device.screen

local ICONS = (Device:isKobo() and "/mnt/onboard" or os.getenv("VOXIE_LOOK_ROOT") or ".") .. "/.adds/voxie/look/"

local GRAY = Blitbuffer.gray(0.7)        -- Kobo's header and footer ink
local TRACK = Blitbuffer.gray(0.2)        -- the unread part of the progress line
local function px(n) return Screen:scaleBySize(n) end

-- ---------------------------------------------------------------------------
-- Header and footer

local function reflowable(view)
    return view.ui and view.ui.rolling and view.ui.document and view.view_mode == "page"
end

local function chrome(view)
    local ui, doc = view.ui, view.ui.document
    local page, total = doc:getCurrentPage(), doc:getPageCount()
    local toc = ui.toc
    local chapter = toc and toc:getTocTitleByPage(page)
    local chapter_line
    if chapter and chapter ~= "" then
        local done, count = toc:getChapterPagesDone(page), toc:getChapterPageCount(page)
        chapter_line = done and count and T(_("%1 · %2 OF %3"), chapter:upper(), done + 1, count)
            or chapter:upper()
    end
    local props = ui.doc_props or {}
    local title = (props.display_title or props.title or ""):upper()
    local book_line = title ~= "" and T(_("%1 · %2 OF %3"), title, page, total)
        or T(_("%1 OF %2"), page, total)
    return chapter_line, book_line, total > 0 and page / total or 0
end

local function paintCentered(bb, text, x, y, w, face)
    local tw = TextWidget:new{ text = text, face = face, fgcolor = GRAY, max_width = w - px(60), padding = 0 }
    local size = tw:getSize()
    tw:paintTo(bb, x + math.floor((w - size.w) / 2), y - math.floor(size.h / 2))
    tw:free()
end

-- Room for them, on top of the book's own margins, the way KOReader makes room
-- for its own footer: a book whose margins were set before keeps them.
local BAND = 24 -- unscaled, like the margins themselves

local onSetPageMargins = ReaderTypeset.onSetPageMargins
ReaderTypeset.onSetPageMargins = function(self, margins, when_applied_callback)
    if not self.ui.rolling then return onSetPageMargins(self, margins, when_applied_callback) end
    return onSetPageMargins(self, { margins[1], margins[2] + BAND, margins[3], margins[4] + BAND },
                            when_applied_callback)
end

local paintTo = ReaderView.paintTo
ReaderView.paintTo = function(self, bb, x, y)
    paintTo(self, bb, x, y)
    if not reflowable(self) then return end
    local ok, err = pcall(function()
        local w, h = self.dimen.w, self.dimen.h
        local face = Font:getFace("cfont", 11)
        local chapter_line, book_line, fraction = chrome(self)
        if chapter_line then paintCentered(bb, chapter_line, x, y + px(BAND * 0.85), w, face) end
        paintCentered(bb, book_line, x, y + h - px(BAND * 0.85), w, face)
        local line_h = math.max(1, px(1.5))
        bb:paintRect(x, y + h - line_h, w, line_h, TRACK)
        bb:paintRect(x, y + h - line_h, math.floor(w * fraction), line_h, Blitbuffer.COLOR_BLACK)
    end)
    if not ok then logger.warn("Voxie Kobo look: header/footer:", err) end
end

-- ---------------------------------------------------------------------------
-- The toolbar

-- An icon from .adds/voxie/look/ that does something when tapped.
local TapIcon = InputContainer:extend{ file = nil, size = nil, callback = nil, label = nil, label_max_width = nil }

function TapIcon:init()
    local icon = ImageWidget:new{ file = ICONS .. self.file, width = self.size, height = self.size, alpha = true,
                                  is_icon = true }
    local inner = icon
    if self.label then
        inner = HorizontalGroup:new{ align = "center", icon, HorizontalSpan:new{ width = px(8) },
            TextWidget:new{ text = self.label, face = Font:getFace("cfont", 15), bold = true,
                            max_width = self.label_max_width } }
    end
    self[1] = FrameContainer:new{ bordersize = 0, padding = px(8), inner }
    self.dimen = Geom:new{ x = 0, y = 0, w = self[1]:getSize().w, h = self[1]:getSize().h }
    self.ges_events = { Tap = { GestureRange:new{ ges = "tap", range = function() return self.dimen end } } }
end

function TapIcon:onTap()
    if self.callback then self.callback() end
    return true
end

local KoboToolbar = InputContainer:extend{ ui = nil, modal = true }

function KoboToolbar:init()
    local sw, sh = Screen:getWidth(), Screen:getHeight()
    self.dimen = Geom:new{ x = 0, y = 0, w = sw, h = sh }
    local ui, doc = self.ui, self.ui.document
    local icon = px(30)
    local function close_then(fn)
        return function()
            UIManager:close(self, "ui")
            if fn then UIManager:nextTick(fn) end
        end
    end
    local function send(name, arg) return close_then(function() ui:handleEvent(Event:new(name, arg)) end) end

    -- Top: time and battery, then Back on the left and the tools on the right.
    local battery = ""
    local powerd = Device:getPowerDevice()
    if Device:hasBattery() then battery = powerd:getCapacity() .. "%" end
    local small = Font:getFace("cfont", 11)
    local status = OverlapGroup:new{ dimen = Geom:new{ w = sw - 2 * px(16), h = px(24) },
        TextWidget:new{ text = datetime.secondsToHour(os.time(), G_reader_settings:isTrue("twelve_hour_clock")), face = small },
        TextWidget:new{ text = battery, face = small, overlap_align = "right" },
    }
    local tools = HorizontalGroup:new{ align = "center",
        TapIcon:new{ file = "sun.svg", size = icon, callback = send("ShowFlDialog") },
        HorizontalSpan:new{ width = px(14) },
        TapIcon:new{ file = "type.svg", size = icon, callback = send("ShowConfigMenu") },
        HorizontalSpan:new{ width = px(14) },
        TapIcon:new{ file = "gear.svg", size = icon, callback = send("ShowMenu") },
    }
    local back = TapIcon:new{ file = "back.svg", size = icon, label = _("Back"), callback = send("Home") }
    local top_row = OverlapGroup:new{ dimen = Geom:new{ w = sw - 2 * px(16), h = math.max(back:getSize().h, tools:getSize().h) },
        back,
        HorizontalGroup:new{ overlap_align = "right", tools },
    }
    self.top = FrameContainer:new{
        bordersize = 0, padding = 0, padding_left = px(16), padding_right = px(16), padding_top = px(6),
        background = Blitbuffer.COLOR_WHITE,
        VerticalGroup:new{ align = "left", status, top_row, VerticalSpan:new{ width = px(6) } },
    }
    local top_w = FrameContainer:new{ bordersize = 0, padding = 0, VerticalGroup:new{
        self.top, LineWidget:new{ dimen = Geom:new{ w = sw, h = Size.line.thin }, background = Blitbuffer.COLOR_GRAY } } }

    -- Bottom: the page, a slider between chapter jumps, the chapters and the audiobook.
    local page, total = doc:getCurrentPage(), doc:getPageCount()
    local bar_w = sw - 2 * px(16) - 2 * (icon + px(16)) - px(20)
    self.slider = { x = 0, w = bar_w }
    local fraction = total > 0 and page / total or 0
    local knob = px(22)
    local slider = InputContainer:new{ dimen = Geom:new{ w = bar_w, h = knob + px(16) } }
    slider[1] = OverlapGroup:new{ dimen = Geom:new{ w = bar_w, h = knob + px(16) },
        FrameContainer:new{ bordersize = 0, padding = 0, padding_top = math.floor((knob + px(16)) / 2),
            LineWidget:new{ dimen = Geom:new{ w = bar_w, h = Size.line.medium }, background = Blitbuffer.COLOR_GRAY } },
        FrameContainer:new{ bordersize = 0, padding = 0, padding_top = math.floor((knob + px(16)) / 2) - 1,
            LineWidget:new{ dimen = Geom:new{ w = math.max(1, math.floor(bar_w * fraction)), h = Size.line.thick },
                            background = Blitbuffer.COLOR_BLACK } },
        FrameContainer:new{ bordersize = 0, padding = 0, padding_top = px(8),
            overlap_offset = { math.max(0, math.min(bar_w - knob, math.floor(bar_w * fraction - knob / 2))), 0 },
            ImageWidget:new{ file = ICONS .. "knob.svg", width = knob, height = knob, alpha = true, is_icon = true } },
    }
    slider.ges_events = { Tap = { GestureRange:new{ ges = "tap", range = function() return slider.dimen end } } }
    slider.onTap = function(_, _, ges)
        local pct = (ges.pos.x - slider.dimen.x) / slider.dimen.w
        close_then(function() ui:handleEvent(Event:new("GotoPercent", math.max(0, math.min(100, pct * 100)))) end)()
        return true
    end
    local slider_row = HorizontalGroup:new{ align = "center",
        TapIcon:new{ file = "chapter.prev.svg", size = icon, callback = send("GotoPrevChapter") },
        HorizontalSpan:new{ width = px(10) },
        slider,
        HorizontalSpan:new{ width = px(10) },
        TapIcon:new{ file = "chapter.next.svg", size = icon, callback = send("GotoNextChapter") },
    }
    local chapter = ui.toc and ui.toc:getTocTitleByPage(page)
    -- The chapter name stops short of "Audiobook".
    local contents = TapIcon:new{ file = "contents.svg", size = icon,
        label = (chapter and chapter ~= "") and chapter or _("Contents"), callback = send("ShowToc"),
        label_max_width = math.floor((sw - 2 * px(16)) * 0.6) - icon - px(24) }
    local bottom_row = OverlapGroup:new{ dimen = Geom:new{ w = sw - 2 * px(16), h = contents:getSize().h },
        contents,
        TapIcon:new{ file = "headphones.svg", size = icon, label = _("Audiobook"), overlap_align = "right",
                     callback = close_then(function() self:showAudiobook() end) },
    }
    self.bottom = FrameContainer:new{
        bordersize = 0, padding = 0, padding_left = px(16), padding_right = px(16), padding_top = px(10), padding_bottom = px(8),
        background = Blitbuffer.COLOR_WHITE,
        VerticalGroup:new{ align = "center",
            TextWidget:new{ text = T(_("PAGE %1 OF %2"), page, total), face = Font:getFace("cfont", 11) },
            VerticalSpan:new{ width = px(4) },
            slider_row,
            VerticalSpan:new{ width = px(6) },
            bottom_row,
        },
    }
    local bottom_w = FrameContainer:new{ bordersize = 0, padding = 0, VerticalGroup:new{
        LineWidget:new{ dimen = Geom:new{ w = sw, h = Size.line.thin }, background = Blitbuffer.COLOR_GRAY }, self.bottom } }

    local bottom_h = bottom_w:getSize().h
    self[1] = OverlapGroup:new{ dimen = self.dimen,
        top_w,
        FrameContainer:new{ bordersize = 0, padding = 0, overlap_offset = { 0, sh - bottom_h }, bottom_w },
    }
    self.top_h, self.bottom_y = top_w:getSize().h, sh - bottom_h
    self.ges_events = { TapClose = { GestureRange:new{ ges = "tap", range = self.dimen } } }
    if Device:hasKeys() then self.key_events = { Close = { { "Back" } } } end
end

-- A tap on the page between the two bars closes them, as on a Kobo; a tap on a
-- bar's background does nothing.
function KoboToolbar:onTapClose(_, ges)
    if ges.pos.y > self.top_h and ges.pos.y < self.bottom_y then
        UIManager:close(self, "ui")
    end
    return true
end

function KoboToolbar:onClose()
    UIManager:close(self, "ui")
    return true
end

function KoboToolbar:onShow()
    UIManager:setDirty(self, "ui")
end

-- Voxie's own menu entries (librarian_sync.koplugin), as a short list of buttons.
function KoboToolbar:showAudiobook()
    local plugin = self.ui.librarian_sync  -- plugins are keyed by folder name
    if not plugin or not plugin.addToMainMenu then
        UIManager:show(require("ui/widget/infomessage"):new{ text = _("The Voxie plugin is not installed."), timeout = 3 })
        return
    end
    local items = {}
    plugin:addToMainMenu(items)
    local buttons = {}
    local dialog
    for _, item in ipairs(items.librarian_sync.sub_item_table) do
        local enabled = not item.enabled_func or item.enabled_func()
        table.insert(buttons, { {
            text = item.text_func and item.text_func() or item.text,
            enabled = enabled,
            align = "left",
            callback = function()
                UIManager:close(dialog)
                item.callback()
            end,
        } })
    end
    dialog = require("ui/widget/buttondialog"):new{ title = _("Audiobook"), title_align = "center", buttons = buttons }
    UIManager:show(dialog)
end

-- ---------------------------------------------------------------------------
-- The tap that opens it: the middle of the page, between the page-turn sides.
-- Taps on links and highlights still come first; KOReader's own menus keep
-- the top and bottom edges.

local ZONE = "voxie_kobo_toolbar"

local function orderZones(ui)
    for _, first in ipairs({ "tap_link", "readerhighlight_tap" }) do
        if ui._zones[first] then ui.touch_zone_dg:addNodeDep(ZONE, first) end
    end
    ui._ordered_touch_zones = {}
    for _, id in ipairs(ui.touch_zone_dg:serialize()) do
        table.insert(ui._ordered_touch_zones, ui._zones[id])
    end
end

-- A zone that registers again (links do on some settings) drops the others'
-- dependencies on it: put links and highlights back in front each time.
local registerTouchZones = ReaderUI.registerTouchZones
ReaderUI.registerTouchZones = function(self, zones)
    registerTouchZones(self, zones)
    if self._zones and self._zones[ZONE] then orderZones(self) end
end

local init = ReaderUI.init
ReaderUI.init = function(self, ...)
    init(self, ...)
    if not Device:isTouchDevice() or not self.rolling then return end
    self:registerTouchZones({ {
        id = ZONE,
        ges = "tap",
        screen_zone = { ratio_x = 1 / 3, ratio_y = 1 / 8, ratio_w = 1 / 3, ratio_h = 6 / 8 },
        overrides = { "tap_forward", "tap_backward", "readerfooter_tap" },
        handler = function()
            UIManager:show(KoboToolbar:new{ ui = self })
            return true
        end,
    } })
end

-- ---------------------------------------------------------------------------
-- My Books: Cover Browser's defaults, only where nothing was chosen. Kept in
-- memory, never written to its database, so deleting this file undoes it.

local COVER_DEFAULTS = {
    hide_file_info = true,
    no_hint_description = true,
    filemanager_display_mode = "list_image_meta",
    history_display_mode = "mosaic_image",
    collection_display_mode = "mosaic_image",
}

userpatch.registerPatchPluginFunc("coverbrowser", function()
    local ok, BookInfoManager = pcall(require, "bookinfomanager")
    if not ok or BookInfoManager._voxie_kobo_look then return end
    BookInfoManager._voxie_kobo_look = true
    local getSetting = BookInfoManager.getSetting
    BookInfoManager.getSetting = function(self, key)
        local value = getSetting(self, key)
        if value == nil then return COVER_DEFAULTS[key] end
        return value
    end
end)

-- The library's title says what the tab says. Simple UI's English falls back
-- to KOReader's own catalogue, where "Library" is not used by anything else;
-- another language keeps Simple UI's own translation.
do
    local GetText = require("gettext")
    local lang = G_reader_settings:readSetting("language") or "en"
    if lang:match("^en") and GetText.translation["Library"] == nil then
        GetText.translation["Library"] = "My Books"
    end
end
