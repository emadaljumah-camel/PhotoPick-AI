local LrApplication = import "LrApplication"
local LrDate = import "LrDate"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local Settings = require "Settings"
local Logger = require "Logger"
local Dialogs = require "Dialogs"
local Json = require "Json"
local HttpClient = require "HttpClient"
local ExportPreviews = require "ExportPreviews"

local function safeString(value)
    if value == nil then return "" end
    return tostring(value)
end

local function getFormattedMetadata(photo, key)
    local ok, value = pcall(function() return photo:getFormattedMetadata(key) end)
    if ok then return value end
    return nil
end

local function getRawMetadata(photo, key)
    local ok, value = pcall(function() return photo:getRawMetadata(key) end)
    if ok then return value end
    return nil
end

local function currentPickStatus(photo)
    local value = getRawMetadata(photo, "pickStatus")
    if value == 1 then return "picked" end
    if value == -1 then return "rejected" end
    return "unflagged"
end

local function makeJobFolder(root)
    local stamp = os.date("%Y%m%d_%H%M%S")
    local uuid = tostring(math.random(100000, 999999)) .. tostring(os.time())
    local folder = LrPathUtils.child(root, "job_" .. stamp .. "_" .. uuid)
    LrFileUtils.createAllDirectories(folder)
    return folder
end

local function buildManifest(jobId, photos, exported, skipped, settings)
    local photoList = {}
    local photoById = {}
    for _, photo in ipairs(photos) do
        local exportInfo = exported[photo]
        if exportInfo then
            local path = getRawMetadata(photo, "path")
            local filename = ""
            if path and path ~= "" then
                filename = LrPathUtils.leafName(path)
            else
                filename = safeString(getRawMetadata(photo, "fileName"))
                if filename == "" then
                    filename = safeString(getFormattedMetadata(photo, "fileName"))
                end
            end
            if filename == "" then
                filename = safeString(exportInfo.original_filename)
            end
            if filename == "" then
                filename = exportInfo.preview_filename
            end
            local entry = {
                photo_id = exportInfo.photo_id,
                original_path = safeString(path),
                filename = filename,
                capture_time = safeString(getRawMetadata(photo, "dateTimeOriginal")),
                rating = getRawMetadata(photo, "rating") or 0,
                color_label = safeString(getRawMetadata(photo, "colorNameForLabel") or getFormattedMetadata(photo, "label")),
                current_pick_status = currentPickStatus(photo),
                preview_filename = exportInfo.preview_filename,
                exif = {
                    iso = getRawMetadata(photo, "isoSpeedRating"),
                    shutter_speed = safeString(getFormattedMetadata(photo, "shutterSpeed")),
                    aperture = safeString(getFormattedMetadata(photo, "aperture")),
                    focal_length = safeString(getFormattedMetadata(photo, "focalLength")),
                    camera = safeString(getFormattedMetadata(photo, "cameraModel")),
                    lens = safeString(getFormattedMetadata(photo, "lens")),
                },
            }
            table.insert(photoList, entry)
            photoById[entry.photo_id] = { photo = photo, filename = filename, preview_filename = exportInfo.preview_filename }
        end
    end
    return {
        client = "lightroom",
        plugin_version = Settings.VERSION,
        job_id = jobId,
        settings = {
            preview_long_edge = settings.preview_long_edge,
            auto_reject_non_selected = settings.auto_reject_non_selected,
        },
        photos = photoList,
        skipped = skipped,
    }, photoById
end

local function validateResult(result, photoById)
    if type(result) ~= "table" or result.status ~= "completed" or type(result.groups) ~= "table" then
        return false, "Server result was not completed or had no groups list."
    end
    local anyRepresentative = false
    for _, group in ipairs(result.groups) do
        local reps = group.representatives or {}
        local rejected = group.rejected or {}
        local rejectedSet = {}
        if #reps == 0 then
            Logger.log("Group has no representative: " .. safeString(group.group_id))
        end
        for _, photoId in ipairs(rejected) do
            if not photoById[photoId] then
                return false, "Server returned rejected photo_id not in selection: " .. safeString(photoId)
            end
            rejectedSet[photoId] = true
        end
        for _, photoId in ipairs(reps) do
            if not photoById[photoId] then
                return false, "Server returned representative photo_id not in selection: " .. safeString(photoId)
            end
            if rejectedSet[photoId] then
                return false, "Server returned a photo as both representative and rejected: " .. safeString(photoId)
            end
            anyRepresentative = true
        end
    end
    if not anyRepresentative then
        return false, "Server result did not contain any representative photos."
    end
    return true, "ok"
end

local function applyFlags(catalog, result, photoById, settings)
    catalog:withWriteAccessDo("PhotoPickAI Best Similar Picker", function()
        for _, group in ipairs(result.groups or {}) do
            for _, photoId in ipairs(group.representatives or {}) do
                local entry = photoById[photoId]
                if entry then
                    Logger.log("Before pick " .. photoId .. " " .. currentPickStatus(entry.photo))
                    entry.photo:setRawMetadata("pickStatus", 1)
                    Logger.log("After pick " .. photoId)
                end
            end
            if settings.auto_reject_non_selected then
                for _, photoId in ipairs(group.rejected or {}) do
                    local entry = photoById[photoId]
                    if entry then
                        Logger.log("Before reject " .. photoId .. " " .. currentPickStatus(entry.photo))
                        entry.photo:setRawMetadata("pickStatus", -1)
                        Logger.log("After reject " .. photoId)
                    end
                end
            end
        end
    end)
end

LrTasks.startAsyncTask(function()
    math.randomseed(os.time())
    local settings = Settings.load()
    Logger.init(settings.temp_job_root)
    local catalog = LrApplication.activeCatalog()
    local photos = catalog:getTargetPhotos()

    if #photos < settings.min_selected_photos then
        Dialogs.warning("PhotoPickAI", "Select at least " .. tostring(settings.min_selected_photos) .. " photos.")
        return
    end

    local progressScope = LrProgressScope {
        title = "PhotoPickAI: Pick Best Among Similar Photos",
        caption = "Preparing Lightroom selection...",
    }
    progressScope:setPortionComplete(0, 100)

    local jobFolder = makeJobFolder(settings.temp_job_root)
    local previewFolder = LrPathUtils.child(jobFolder, "previews")
    local jobJson = LrPathUtils.child(jobFolder, "job.json")
    local resultJson = LrPathUtils.child(jobFolder, "result.json")
    local jobId = LrPathUtils.leafName(jobFolder):gsub("^job_", "")
    Logger.log("Created job folder " .. jobFolder)
    Logger.logTable("Settings", {
        server_url = settings.server_url,
        dry_run = settings.dry_run,
        helper_python = settings.helper_python,
        helper_script = settings.helper_script,
    })

    progressScope:setCaption("Exporting Lightroom JPEG previews...")
    progressScope:setPortionComplete(5, 100)
    local exported, skipped = ExportPreviews.export(photos, previewFolder, settings, Logger, progressScope)
    if progressScope:isCanceled() then
        Logger.log("User canceled during preview export")
        progressScope:done()
        return
    end

    progressScope:setCaption("Writing PhotoPickAI job manifest...")
    progressScope:setPortionComplete(45, 100)
    local manifest, photoById = buildManifest(jobId, photos, exported, skipped, settings)
    Json.writeFile(jobJson, manifest)
    Logger.log("Wrote manifest " .. jobJson .. " photos=" .. tostring(#manifest.photos))

    if #manifest.photos < settings.min_selected_photos then
        progressScope:done()
        Dialogs.warning("PhotoPickAI", "Fewer than two selected photos could be exported as previews. No flags were changed.")
        return
    end

    progressScope:setCaption("Sending previews to PhotoPickAI server and waiting for review...")
    progressScope:setPortionComplete(55, 100)
    Logger.log("Helper command " .. HttpClient.buildCommand(settings, jobJson, previewFolder, resultJson))
    local exitCode = HttpClient.runHelper(settings, jobJson, previewFolder, resultJson)
    Logger.log("Helper exit code " .. tostring(exitCode))
    if exitCode ~= 0 then
        progressScope:done()
        Dialogs.error(
            "PhotoPickAI Server Error",
            "The PhotoPickAI helper failed. No flags were changed.\n\nJob folder:\n" .. jobFolder .. "\n\nPlugin log:\n" .. Logger.path
        )
        return
    end

    progressScope:setCaption("Reading PhotoPickAI result...")
    progressScope:setPortionComplete(85, 100)
    local ok, result = pcall(function() return Json.readFile(resultJson) end)
    if not ok then
        Logger.log("Invalid result JSON: " .. tostring(result))
        progressScope:done()
        Dialogs.error("PhotoPickAI Invalid Result", "Could not read server result JSON. No flags were changed.")
        return
    end

    progressScope:setCaption("Validating PhotoPickAI decisions...")
    progressScope:setPortionComplete(90, 100)
    local valid, message = validateResult(result, photoById)
    Logger.log("Validation " .. tostring(valid) .. " " .. message)
    if not valid then
        progressScope:done()
        Dialogs.error("PhotoPickAI Invalid Result", message .. "\nNo flags were changed.")
        return
    end

    if settings.dry_run then
        progressScope:setCaption("PhotoPickAI dry run complete.")
        progressScope:setPortionComplete(100, 100)
        progressScope:done()
        Dialogs.summary(result, photoById, true)
        return
    end

    progressScope:setCaption("Applying Lightroom pick/reject flags...")
    progressScope:setPortionComplete(95, 100)
    applyFlags(catalog, result, photoById, settings)
    progressScope:setCaption("PhotoPickAI finished.")
    progressScope:setPortionComplete(100, 100)
    progressScope:done()
    Dialogs.summary(result, photoById, false)
end)
