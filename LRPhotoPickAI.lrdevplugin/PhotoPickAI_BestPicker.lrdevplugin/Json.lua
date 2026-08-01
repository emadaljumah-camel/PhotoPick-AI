local Json = {}

local function escapeString(value)
    value = value:gsub("\\", "\\\\")
    value = value:gsub('"', '\\"')
    value = value:gsub("\n", "\\n")
    value = value:gsub("\r", "\\r")
    value = value:gsub("\t", "\\t")
    return '"' .. value .. '"'
end

local function isArray(value)
    local count = 0
    for key, _ in pairs(value) do
        if type(key) ~= "number" then
            return false
        end
        count = count + 1
    end
    return count == #value
end

function Json.encode(value)
    local kind = type(value)
    if kind == "nil" then
        return "null"
    elseif kind == "boolean" or kind == "number" then
        return tostring(value)
    elseif kind == "string" then
        return escapeString(value)
    elseif kind == "table" then
        local items = {}
        if isArray(value) then
            for _, item in ipairs(value) do
                table.insert(items, Json.encode(item))
            end
            return "[" .. table.concat(items, ",") .. "]"
        end
        for key, item in pairs(value) do
            table.insert(items, escapeString(tostring(key)) .. ":" .. Json.encode(item))
        end
        return "{" .. table.concat(items, ",") .. "}"
    end
    error("Unsupported JSON value type: " .. kind)
end

local Parser = {}
Parser.__index = Parser

function Parser:new(text)
    return setmetatable({ text = text, index = 1 }, self)
end

function Parser:peek()
    return self.text:sub(self.index, self.index)
end

function Parser:skipWhitespace()
    while self:peek():match("%s") do
        self.index = self.index + 1
    end
end

function Parser:consume(expected)
    local actual = self:peek()
    if expected and actual ~= expected then
        error("Expected " .. expected .. " at JSON index " .. self.index)
    end
    self.index = self.index + 1
    return actual
end

function Parser:parseString()
    self:consume('"')
    local out = {}
    while true do
        local char = self:consume()
        if char == '"' then
            return table.concat(out)
        elseif char == "\\" then
            local escaped = self:consume()
            local map = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
            table.insert(out, map[escaped] or escaped)
        else
            table.insert(out, char)
        end
    end
end

function Parser:parseNumber()
    local start = self.index
    while self:peek():match("[%d%+%-%.eE]") do
        self.index = self.index + 1
    end
    return tonumber(self.text:sub(start, self.index - 1))
end

function Parser:parseArray()
    local out = {}
    self:consume("[")
    self:skipWhitespace()
    if self:peek() == "]" then
        self:consume("]")
        return out
    end
    while true do
        table.insert(out, self:parseValue())
        self:skipWhitespace()
        local char = self:consume()
        if char == "]" then
            return out
        elseif char ~= "," then
            error("Expected comma or array end")
        end
    end
end

function Parser:parseObject()
    local out = {}
    self:consume("{")
    self:skipWhitespace()
    if self:peek() == "}" then
        self:consume("}")
        return out
    end
    while true do
        self:skipWhitespace()
        local key = self:parseString()
        self:skipWhitespace()
        self:consume(":")
        out[key] = self:parseValue()
        self:skipWhitespace()
        local char = self:consume()
        if char == "}" then
            return out
        elseif char ~= "," then
            error("Expected comma or object end")
        end
    end
end

function Parser:parseLiteral(text, value)
    if self.text:sub(self.index, self.index + #text - 1) ~= text then
        error("Expected " .. text)
    end
    self.index = self.index + #text
    return value
end

function Parser:parseValue()
    self:skipWhitespace()
    local char = self:peek()
    if char == '"' then
        return self:parseString()
    elseif char == "{" then
        return self:parseObject()
    elseif char == "[" then
        return self:parseArray()
    elseif char == "t" then
        return self:parseLiteral("true", true)
    elseif char == "f" then
        return self:parseLiteral("false", false)
    elseif char == "n" then
        return self:parseLiteral("null", nil)
    end
    return self:parseNumber()
end

function Json.decode(text)
    return Parser:new(text):parseValue()
end

function Json.writeFile(path, value)
    local file = assert(io.open(path, "w"))
    file:write(Json.encode(value))
    file:close()
end

function Json.readFile(path)
    local file = assert(io.open(path, "r"))
    local text = file:read("*a")
    file:close()
    return Json.decode(text)
end

return Json
