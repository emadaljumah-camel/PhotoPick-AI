local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"

local Logger = {}
local MAX_LOG_BYTES = 5 * 1024 * 1024

local function now()
    return os.date("%Y-%m-%d %H:%M:%S")
end

function Logger.init(root)
    LrFileUtils.createAllDirectories(root)
    Logger.path = LrPathUtils.child(root, "plugin.log")
    local file = io.open(Logger.path, "rb")
    if file then
        local size = file:seek("end") or 0
        file:close()
        if size > MAX_LOG_BYTES then
            local archive = LrPathUtils.child(root, "plugin_" .. os.date("%Y%m%d_%H%M%S") .. ".log")
            os.rename(Logger.path, archive)
        end
    end
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
