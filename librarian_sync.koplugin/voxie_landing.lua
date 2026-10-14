-- The landing rules, on the device: core/landing.py in Lua.
--
-- Where a page lands in the audio, and where an audio time lands in the book,
-- from the copy of the map the plugin keeps, so they work with no network.
-- tools/landing_examples.json is the one set of examples this file and the
-- gateway's Python are both tested against (tools/test_landing.lua and
-- tools/test_landing.py): change a rule in both, or the tests fail.
--
-- A map is positions only, never text:
--   nodes     { {path, start, end}, ... } sorted by start
--   chapters  { {title, start, end}, ... } the audiobook's chapters, one per
--             stitched file, as Audiobookshelf shows them
--
-- Named voxie_landing, not landing: KOReader puts every plugin's folder on
-- one package.path, so a common name could load another plugin's file.

local M = {}

-- Seconds, not words: words vary in length and seconds do not.
M.ROLLBACK_SECONDS = 3.0
-- A chapter's first paragraph can start a little after the chapter itself.
M.CHAPTER_SLACK = 0.5

function M.kobo_path(stored)
    local path = (stored or ""):gsub("^point%(/1", "", 1)
    return (path:gsub("%)+$", ""))
end

-- KOReader's position with its offsets cut: the paragraph it starts in.
function M.paragraph_path(raw)
    local path = M.kobo_path(raw)
    path = path:gsub("/span%[%d+%].*", "")
    path = path:gsub("/text%(%).*", "")
    return path
end

local function slashes(s)
    return select(2, s:gsub("/", ""))
end

-- (1-based chapter number, seconds into it).
function M.chapter_at(chapters, seconds)
    if not chapters or #chapters == 0 then return 1, seconds end
    for i, ch in ipairs(chapters) do
        if seconds < ch[3] then return i, math.max(0, seconds - ch[2]) end
    end
    return #chapters, math.max(0, seconds - chapters[#chapters][2])
end

-- start, end of the paragraph a position is in, or nil. Earliest node wins;
-- a position inside inline markup walks up to the nearest mapped ancestor.
function M.time_for_position(nodes, position)
    local first = {}
    for _, n in ipairs(nodes) do
        if first[n[1]] == nil then first[n[1]] = n end
    end
    local path = M.paragraph_path(position)
    local found = first[path]
    while not found and slashes(path) > 3 do
        path = path:match("^(.*)/[^/]*$")
        found = first[path]
    end
    if not found then return nil end
    return found[2], found[3] or found[2]
end

-- Seconds into the audiobook for a page: its paragraph's start, else the same
-- fraction of the whole. The second value says which (true = from the map).
function M.audio_time(nodes, chapters, position, percentage)
    local start = M.time_for_position(nodes, position)
    if start then return start, true end
    local total = (#chapters > 0) and chapters[#chapters][3] or 0
    return total * (percentage or 0), false
end

-- The paragraph to open for an audio time: 3 s back for context, never across
-- a chapter start. nil for an empty map.
function M.landing_position(nodes, chapters, seconds, rollback)
    rollback = rollback or M.ROLLBACK_SECONDS
    if not nodes or #nodes == 0 then return nil end
    local floor = 0
    if chapters and #chapters > 0 then
        floor = chapters[(M.chapter_at(chapters, seconds))][2]
    end
    local target = math.max(seconds - rollback, floor)
    local best
    for _, n in ipairs(nodes) do
        if n[2] <= target then best = n else break end
    end
    if not best then return nodes[1][1] end
    if best[2] < floor - M.CHAPTER_SLACK then
        for _, n in ipairs(nodes) do
            if n[2] >= floor - M.CHAPTER_SLACK then
                if n[2] <= seconds then best = n end
                break
            end
        end
    end
    return best[1]
end

-- 14:32, or 1:02:03 past an hour: how audiobook players show a time.
function M.format_time(seconds)
    local s = math.floor((seconds or 0) + 0.5)
    local h, m = math.floor(s / 3600), math.floor(s % 3600 / 60)
    if h > 0 then return string.format("%d:%02d:%02d", h, m, s % 60) end
    return string.format("%d:%02d", m, s % 60)
end

-- "14:32", "1:02:03" or "90" to seconds; nil if it is not a time.
function M.parse_time(text)
    text = (text or ""):gsub("^%s+", ""):gsub("%s+$", "")
    if text == "" then return nil end
    local total = 0
    local parts = 0
    for part in (text .. ":"):gmatch("([^:]*):") do
        if not part:match("^%d+$") then return nil end
        total = total * 60 + tonumber(part)
        parts = parts + 1
    end
    if parts > 3 then return nil end
    return total
end

return M
