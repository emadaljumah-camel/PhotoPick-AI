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
    return photo:getFormattedMetadata(key)
end

local function getRawMetadata(photo, key)
    return photo:getRawMetadata(key)
end

local function captureTime(photo)
    for _, key in ipairs({ "dateTimeOriginalISO8601", "dateTimeISO8601" }) do
        local value = getRawMetadata(photo, key)
        if value ~= nil and value ~= "" then
            return safeString(value)
        end
    end

    local numericValue = getRawMetadata(photo, "dateTimeOriginal")
        or getRawMetadata(photo, "dateTime")
    if type(numericValue) == "number" then
        local ok, value = pcall(function()
            return LrDate.timeToW3CDate(numericValue)
        end)
        if ok and value then
            return safeString(value)
        end
        Logger.log("Unable to convert numeric Lightroom capture time: " .. safeString(value))
    end
    return ""
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
            local captureTimeValue = captureTime(photo)
            if captureTimeValue == "" then
                Logger.log("Capture time unavailable for " .. filename .. "; server will inspect preview EXIF.")
            end
            local entry = {
                photo_id = exportInfo.photo_id,
                catalog_uuid = safeString(getRawMetadata(photo, "uuid")),
                filename = filename,
                capture_time = captureTimeValue,
                preview_filename = exportInfo.preview_filename,
            }
            table.insert(photoList, entry)
        end
    end
    return {
        client = "lightroom",
        plugin_version = Settings.VERSION,
        job_id = jobId,
        settings = {
            preview_long_edge = settings.preview_long_edge,
        },
        photos = photoList,
        skipped = skipped,
    }
end

LrTasks.startAsyncTask(function()
    math.randomseed(os.time())
    local settings = Settings.load()
    Logger.init(settings.temp_job_root)
    local catalog = LrApplication.activeCatalog()
    local photos = catalog:getTargetPhotos()
    local activeJobPath = LrPathUtils.child(settings.temp_job_root, "active_job.json")

    if LrFileUtils.exists(activeJobPath) then
        local activeOk, activeJob = pcall(function() return Json.readFile(activeJobPath) end)
        if activeOk and type(activeJob) == "table" and not activeJob.applied and not activeJob.closed then
            Dialogs.warning(
                "PhotoPickAI Job Pending",
                "Job " .. safeString(activeJob.job_id)
                    .. " has not been applied yet. Use 'PhotoPickAI: Check Job Status / Apply Results' before submitting another job."
            )
            return
        end
    end

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
    local manifest = buildManifest(jobId, photos, exported, skipped, settings)
    Json.writeFile(jobJson, manifest)
    Logger.log("Wrote manifest " .. jobJson .. " photos=" .. tostring(#manifest.photos))

    if #manifest.photos < settings.min_selected_photos then
        progressScope:done()
        Dialogs.warning("PhotoPickAI", "Fewer than two selected photos could be exported as previews. No flags were changed.")
        return
    end

    progressScope:setCaption("Uploading previews and starting PhotoPickAI job...")
    progressScope:setPortionComplete(55, 100)
    Logger.log("Helper command " .. HttpClient.buildCommand(settings, jobJson, previewFolder, resultJson, "submit"))
    local exitCode = HttpClient.runHelper(settings, jobJson, previewFolder, resultJson, "submit")
    Logger.log("Helper exit code " .. tostring(exitCode))
    if exitCode ~= 0 then
        progressScope:done()
        Dialogs.error(
            "PhotoPickAI Server Error",
            "The PhotoPickAI helper failed. No flags were changed.\n\nJob folder:\n" .. jobFolder .. "\n\nPlugin log:\n" .. Logger.path
        )
        return
    end

    progressScope:setCaption("Recording PhotoPickAI job...")
    progressScope:setPortionComplete(90, 100)
    local ok, response = pcall(function() return Json.readFile(resultJson) end)
    if not ok then
        Logger.log("Invalid submission response JSON: " .. tostring(response))
        progressScope:done()
        Dialogs.error("PhotoPickAI Invalid Response", "Could not read the server submission response. No flags were changed.")
        return
    end

    if type(response) ~= "table" or not response.job_id or not response.status then
        progressScope:done()
        Dialogs.error("PhotoPickAI Invalid Response", "The server did not return a job ID and status. No flags were changed.")
        return
    end

    Json.writeFile(activeJobPath, {
        schema_version = 1,
        job_id = response.job_id,
        job_folder = jobFolder,
        catalog_path = safeString(catalog:getPath()),
        server_url = settings.server_url,
        dry_run = settings.dry_run,
        auto_reject_non_selected = settings.auto_reject_non_selected,
        applied = false,
        submitted_at = os.time(),
    })
    Logger.log("Recorded active job " .. safeString(response.job_id) .. " at " .. activeJobPath)
    progressScope:setCaption("PhotoPickAI job submitted.")
    progressScope:setPortionComplete(100, 100)
    progressScope:done()
    Dialogs.info(
        "PhotoPickAI Job Submitted",
        "Job " .. safeString(response.job_id) .. " is processing on the server.\n\n"
            .. "Use 'PhotoPickAI: Check Job Status / Apply Results' later."
    )
end)
