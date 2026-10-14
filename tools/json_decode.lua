-- Enough JSON for the tests' own files: objects, arrays, strings, numbers,
-- booleans. Plain LuaJIT has no JSON module; KOReader has rapidjson and json.
--
--     local decode = dofile("tools/json_decode.lua")

local function decode(s)
    local i = 1
    local function ws() i = s:find("[^%s]", i) or #s + 1 end
    local value
    local function str()
        local out, j = {}, i + 1
        while true do
            local c = s:sub(j, j)
            if c == '"' then i = j + 1; return table.concat(out) end
            if c == "\\" then
                local e = s:sub(j + 1, j + 1)
                out[#out + 1] = ({ n = "\n", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" })[e] or e
                j = j + 2
            else
                out[#out + 1] = c; j = j + 1
            end
        end
    end
    function value()
        ws()
        local c = s:sub(i, i)
        if c == "{" then
            local t = {}; i = i + 1; ws()
            if s:sub(i, i) == "}" then i = i + 1; return t end
            while true do
                ws(); local k = str(); ws(); i = i + 1   -- the colon
                t[k] = value(); ws()
                local d = s:sub(i, i); i = i + 1
                if d == "}" then return t end
            end
        elseif c == "[" then
            local t = {}; i = i + 1; ws()
            if s:sub(i, i) == "]" then i = i + 1; return t end
            while true do
                t[#t + 1] = value(); ws()
                local d = s:sub(i, i); i = i + 1
                if d == "]" then return t end
            end
        elseif c == '"' then return str()
        elseif s:sub(i, i + 3) == "true" then i = i + 4; return true
        elseif s:sub(i, i + 4) == "false" then i = i + 5; return false
        else
            local num = s:match("^-?[%d%.eE+-]+", i)
            i = i + #num
            return tonumber(num)
        end
    end
    return value()
end

return decode
