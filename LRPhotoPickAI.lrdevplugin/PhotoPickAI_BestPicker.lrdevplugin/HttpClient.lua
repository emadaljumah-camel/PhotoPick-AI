local LrTasks = import "LrTasks"

local HttpClient = {}

local function quote(value)
    value = tostring(value)
    if not value:find("[%s&%(%)%%%!%^%[%]{}=;,'`~]") then
        return value
    end
    return '"' .. value:gsub('"', '\\"') .. '"'
end

function HttpClient.buildCommand(settings, jobJsonPath, previewFolder, resultJsonPath, action)
    local parts = {
        quote(settings.helper_python),
        quote(settings.helper_script),
        "--server-url", quote(settings.server_url),
        "--job-json", quote(jobJsonPath),
        "--preview-folder", quote(previewFolder),
        "--result-json", quote(resultJsonPath),
        "--timeout", quote(settings.timeout_seconds),
        "--action", quote(action or "submit"),
    }
    if settings.api_key and settings.api_key ~= "" then
        table.insert(parts, "--api-key")
        table.insert(parts, quote(settings.api_key))
    end
    return table.concat(parts, " ")
end

function HttpClient.runHelper(settings, jobJsonPath, previewFolder, resultJsonPath, action)
    return LrTasks.execute(HttpClient.buildCommand(settings, jobJsonPath, previewFolder, resultJsonPath, action))
end

return HttpClient
