local LrPrefs = import "LrPrefs"
local LrPathUtils = import "LrPathUtils"

local Settings = {}

Settings.VERSION = "1.0.0"

local pluginRoot = (_PLUGIN and _PLUGIN.path) or ""
local bundledHelperScript = LrPathUtils.child(LrPathUtils.child(pluginRoot, "helper"), "lr_client_helper.py")

Settings.DEFAULTS = {
    server_url = "http://100.108.139.62:8080/photopick",
    api_key = "",
    preview_long_edge = 1600,
    jpeg_quality = 80,
    dry_run = false,
    auto_reject_non_selected = true,
    timeout_seconds = 600,
    poll_interval_seconds = 2,
    max_poll_attempts = 300,
    min_selected_photos = 2,
    helper_python = "python",
    helper_script = bundledHelperScript,
}

function Settings.defaults()
    local defaults = {}
    for key, value in pairs(Settings.DEFAULTS) do
        defaults[key] = value
    end
    defaults.temp_job_root = LrPathUtils.child(LrPathUtils.getStandardFilePath("temp"), "PhotoPickAI_LR_Jobs")
    return defaults
end

function Settings.applyDefaults(target, overwrite)
    local defaults = Settings.defaults()
    for key, value in pairs(defaults) do
        if overwrite or target[key] == nil then
            target[key] = value
        end
    end
    return target
end

function Settings.normalize(target)
    local defaults = Settings.defaults()
    local requiredStrings = {
        "server_url",
        "temp_job_root",
        "helper_python",
        "helper_script",
    }
    local requiredNumbers = {
        "preview_long_edge",
        "jpeg_quality",
        "timeout_seconds",
        "poll_interval_seconds",
        "max_poll_attempts",
        "min_selected_photos",
    }

    for _, key in ipairs(requiredStrings) do
        if target[key] == nil or target[key] == "" then
            target[key] = defaults[key]
        end
    end

    for _, key in ipairs(requiredNumbers) do
        local value = tonumber(target[key])
        if value == nil then
            target[key] = defaults[key]
        else
            target[key] = value
        end
    end

    if target.dry_run == nil then
        target.dry_run = defaults.dry_run
    end
    if target.auto_reject_non_selected == nil then
        target.auto_reject_non_selected = defaults.auto_reject_non_selected
    end

    return target
end

function Settings.load()
    local prefs = LrPrefs.prefsForPlugin()
    Settings.applyDefaults(prefs, false)
    Settings.normalize(prefs)
    return prefs
end

function Settings.reset()
    return Settings.applyDefaults(LrPrefs.prefsForPlugin(), true)
end

return Settings
