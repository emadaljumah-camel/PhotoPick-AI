local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"

local Logger = {}

local function now()
    return os.date("%Y-%m-%d %H:%M:%S")
end

function Logger.init(root)
    LrFileUtils.createAllDirectories(root)
    Logger.path = LrPathUtils.child(root, "plugin.log")
end

function Logger.log(message)
    if not Logger.path then
        return
    end
    local file = io.open(Logger.path, "a")
    if file then
        file:write(string.format("[%s] %s\n", now(), tostring(message)))
        file:close()
    end
end

function Logger.logTable(prefix, values)
    local parts = {}
    for key, value in pairs(values or {}) do
        table.insert(parts, tostring(key) .. "=" .. tostring(value))
    end
    Logger.log(prefix .. " " .. table.concat(parts, " "))
end

return Logger
