local LrBinding = import "LrBinding"
local LrView = import "LrView"

local Settings = require "Settings"

local bind = LrView.bind

return {
    sectionsForTopOfDialog = function(viewFactory, propertyTable)
        local prefs = Settings.load()
        local defaults = Settings.defaults()

        local function refreshPropertyTable()
            for key, _ in pairs(defaults) do
                propertyTable[key] = prefs[key]
            end
        end

        refreshPropertyTable()

        propertyTable:addObserver("server_url", function(_, _, value) prefs.server_url = value or defaults.server_url end)
        propertyTable:addObserver("preview_long_edge", function(_, _, value) prefs.preview_long_edge = tonumber(value) or defaults.preview_long_edge end)
        propertyTable:addObserver("jpeg_quality", function(_, _, value) prefs.jpeg_quality = tonumber(value) or defaults.jpeg_quality end)
        propertyTable:addObserver("dry_run", function(_, _, value) prefs.dry_run = value end)
        propertyTable:addObserver("auto_reject_non_selected", function(_, _, value) prefs.auto_reject_non_selected = value end)
        propertyTable:addObserver("timeout_seconds", function(_, _, value) prefs.timeout_seconds = tonumber(value) or defaults.timeout_seconds end)
        propertyTable:addObserver("api_key", function(_, _, value) prefs.api_key = value or defaults.api_key end)
        propertyTable:addObserver("temp_job_root", function(_, _, value) prefs.temp_job_root = value or defaults.temp_job_root end)
        propertyTable:addObserver("min_selected_photos", function(_, _, value) prefs.min_selected_photos = tonumber(value) or defaults.min_selected_photos end)
        propertyTable:addObserver("helper_python", function(_, _, value) prefs.helper_python = value or defaults.helper_python end)
        propertyTable:addObserver("helper_script", function(_, _, value) prefs.helper_script = value or defaults.helper_script end)

        local function row(label, control)
            return viewFactory:row {
                viewFactory:static_text { title = label, width = 180, alignment = "right" },
                control,
            }
        end

        return {
            {
                title = "PhotoPickAI Server",
                synopsis = bind "server_url",
                viewFactory:column {
                    bind_to_object = propertyTable,
                    spacing = viewFactory:control_spacing(),
                    row("Server URL", viewFactory:edit_field { value = bind "server_url", width_in_chars = 42 }),
                    viewFactory:push_button {
                        title = "Reset to Default Values",
                        action = function()
                            prefs = Settings.reset()
                            defaults = Settings.defaults()
                            refreshPropertyTable()
                        end,
                    },
                },
            },
            {
                title = "Processing",
                viewFactory:column {
                    bind_to_object = propertyTable,
                    spacing = viewFactory:control_spacing(),
                    row("Preview long edge", viewFactory:edit_field { value = bind "preview_long_edge", width_in_chars = 8 }),
                    row("JPEG quality", viewFactory:edit_field { value = bind "jpeg_quality", width_in_chars = 8 }),
                    row("Network request timeout", viewFactory:edit_field { value = bind "timeout_seconds", width_in_chars = 8 }),
                    row("Minimum selected photos", viewFactory:edit_field { value = bind "min_selected_photos", width_in_chars = 8 }),
                    row("Dry run", viewFactory:checkbox { value = bind "dry_run" }),
                    row("Auto reject non-selected", viewFactory:checkbox { value = bind "auto_reject_non_selected" }),
                },
            },
            {
                title = "Advanced",
                viewFactory:column {
                    bind_to_object = propertyTable,
                    spacing = viewFactory:control_spacing(),
                    row("API key", viewFactory:password_field { value = bind "api_key", width_in_chars = 42 }),
                    row("Helper Python", viewFactory:edit_field { value = bind "helper_python", width_in_chars = 42 }),
                    row("Helper script", viewFactory:edit_field { value = bind "helper_script", width_in_chars = 42 }),
                    row("Temp job root", viewFactory:edit_field { value = bind "temp_job_root", width_in_chars = 42 }),
                },
            },
        }
    end,
}
