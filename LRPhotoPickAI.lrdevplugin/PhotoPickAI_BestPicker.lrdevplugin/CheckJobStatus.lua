local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local Settings = require "Settings"
local Logger = require "Logger"
local Dialogs = require "Dialogs"
local Json = require "Json"
local HttpClient = require "HttpClient"

local function safeString(value)
    if value == nil then return "" end
    return tostring(value)
end

local function resolvePhotos(catalog, manifest)
    local wanted = {}
    local photoById = {}
    for _, entry in ipairs(manifest.photos or {}) do
        wanted[safeString(entry.photo_id)] = entry
        local catalogUuid = safeString(entry.catalog_uuid)
        if catalogUuid ~= "" then
            local photo = catalog:findPhotoByUuid(catalogUuid)
            if photo then
                photoById[safeString(entry.photo_id)] = {
                    photo = photo,
                    filename = entry.filename,
                    preview_filename = entry.preview_filename,
                }
            end
        end
    end

    local needsLegacyScan = false
    for photoId, _ in pairs(wanted) do
        if not photoById[photoId] then
            needsLegacyScan = true
            break
        end
    end
    if needsLegacyScan then
        for _, photo in ipairs(catalog:getAllPhotos()) do
            local photoId = safeString(photo.localIdentifier or photo:getRawMetadata("uuid"))
            local entry = wanted[photoId]
            if entry and not photoById[photoId] then
                photoById[photoId] = {
                    photo = photo,
                    filename = entry.filename,
                    preview_filename = entry.preview_filename,
                }
            end
        end
    end

    local missing = {}
    for photoId, _ in pairs(wanted) do
        if not photoById[photoId] then
            table.insert(missing, photoId)
        end
    end
    table.sort(missing)
    return photoById, missing
end

local function validateResult(result, photoById)
    if type(result) ~= "table" or result.status ~= "completed" or type(result.groups) ~= "table" then
        return false, "Server result was not completed or had no groups list."
    end

    local anyRepresentative = false
    for _, group in ipairs(result.groups) do
        local rejectedSet = {}
        for _, photoId in ipairs(group.rejected or {}) do
            if not photoById[photoId] then
                return false, "Server returned rejected photo_id not found in the original catalog: " .. safeString(photoId)
            end
            rejectedSet[photoId] = true
        end
        for _, photoId in ipairs(group.representatives or {}) do
            if not photoById[photoId] then
                return false, "Server returned representative photo_id not found in the original catalog: " .. safeString(photoId)
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

local function applyFlags(catalog, result, photoById, settings, progressScope)
    local total = 0
    for _, group in ipairs(result.groups or {}) do
        total = total + #(group.representatives or {})
        if settings.auto_reject_non_selected then
            total = total + #(group.rejected or {})
        end
    end
    local applied = 0
    local writeStatus = catalog:withWriteAccessDo("PhotoPickAI Best Similar Picker", function()
        progressScope:setCaption("Applying PhotoPickAI flags...")
        for _, group in ipairs(result.groups or {}) do
            for _, photoId in ipairs(group.representatives or {}) do
                local entry = photoById[photoId]
                entry.photo:setRawMetadata("pickStatus", 1)
                applied = applied + 1
                progressScope:setPortionComplete(applied, math.max(total, 1))
            end
            if settings.auto_reject_non_selected then
                for _, photoId in ipairs(group.rejected or {}) do
                    local entry = photoById[photoId]
                    entry.photo:setRawMetadata("pickStatus", -1)
                    applied = applied + 1
                    progressScope:setPortionComplete(applied, math.max(total, 1))
                end
            end
        end
    end, { timeout = 30 })
    Logger.log("Catalog flag apply status=" .. safeString(writeStatus) .. " changed=" .. tostring(applied))
    return writeStatus
end

local function errorText(response)
    if type(response) ~= "table" then return "Unknown server error." end
    local errors = response.errors or {}
    if #errors > 0 then return table.concat(errors, "\n") end
    return "The server reported status " .. safeString(response.status) .. "."
end

LrTasks.startAsyncTask(function()
    local settings = Settings.load()
    Logger.init(settings.temp_job_root)
    local activeJobPath = LrPathUtils.child(settings.temp_job_root, "active_job.json")

    if not LrFileUtils.exists(activeJobPath) then
        Dialogs.info("PhotoPickAI", "There is no active PhotoPickAI job to check.")
        return
    end

    local activeOk, activeJob = pcall(function() return Json.readFile(activeJobPath) end)
    if not activeOk or type(activeJob) ~= "table" or not activeJob.job_folder then
        Dialogs.error("PhotoPickAI", "The active PhotoPickAI job record is invalid.")
        return
    end
    if activeJob.applied then
        Dialogs.info("PhotoPickAI", "Job " .. safeString(activeJob.job_id) .. " has already been applied.")
        return
    end
    if activeJob.closed then
        Dialogs.info(
            "PhotoPickAI",
            "Job " .. safeString(activeJob.job_id) .. " is closed and cannot be applied. Submit a new job."
        )
        return
    end

    local catalog = LrApplication.activeCatalog()
    local currentCatalogPath = safeString(catalog:getPath())
    if activeJob.catalog_path and activeJob.catalog_path ~= "" and activeJob.catalog_path ~= currentCatalogPath then
        Dialogs.error(
            "PhotoPickAI Catalog Mismatch",
            "This job belongs to a different Lightroom catalog. Open the original catalog before applying its results."
        )
        return
    end
    if activeJob.server_url and activeJob.server_url ~= "" then
        settings.server_url = activeJob.server_url
    end

    local jobFolder = activeJob.job_folder
    local previewFolder = LrPathUtils.child(jobFolder, "previews")
    local jobJson = LrPathUtils.child(jobFolder, "job.json")
    local resultJson = LrPathUtils.child(jobFolder, "result.json")
    local function closeActiveJob(status)
        activeJob.closed = true
        activeJob.status = status
        activeJob.closed_at = os.time()
        Json.writeFile(activeJobPath, activeJob)
    end
    if not LrFileUtils.exists(jobJson) then
        closeActiveJob("local_manifest_missing")
        Dialogs.error("PhotoPickAI", "The local job manifest is missing. Submit the photos again as a new job.")
        return
    end

    local function callHelper(action)
        Logger.log("Helper command " .. HttpClient.buildCommand(settings, jobJson, previewFolder, resultJson, action))
        local exitCode = HttpClient.runHelper(settings, jobJson, previewFolder, resultJson, action)
        Logger.log("Helper exit code " .. tostring(exitCode) .. " action=" .. action)
        local responseOk, response = pcall(function() return Json.readFile(resultJson) end)
        if not responseOk then
            return nil, exitCode, "Could not read the PhotoPickAI status response."
        end
        return response, exitCode, nil
    end

    local progressScope = LrProgressScope {
        title = "PhotoPickAI: Check Job Status",
        caption = "Checking PhotoPickAI server...",
    }
    local response, exitCode, helperError = callHelper("check")
    progressScope:done()

    if helperError then
        Dialogs.error("PhotoPickAI Status Error", helperError)
        return
    end
    if exitCode ~= 0 then
        if response and response.status == "missing" then
            closeActiveJob("missing")
            Dialogs.error("PhotoPickAI Job Missing", "The server no longer has this job. Submit the photos again as a new job.")
        else
            Dialogs.error("PhotoPickAI Status Error", errorText(response) .. "\n\nNo flags were changed.")
        end
        return
    end

    local handleResponse
    handleResponse = function(current)
        local status = safeString(current.status)
        if status == "created" or status == "running" then
            local jobProgress = current.progress or {}
            local processed = tonumber(jobProgress.processed_groups) or 0
            local total = tonumber(jobProgress.total_groups) or 0
            local message = "Job " .. safeString(activeJob.job_id) .. " is still running.\n\n"
                .. "Stage: " .. safeString(jobProgress.stage) .. "\n"
                .. "Progress: " .. tostring(processed) .. "/" .. tostring(total)
            if jobProgress.current_group_id then
                message = message .. "\nCurrent group: " .. safeString(jobProgress.current_group_id)
            end
            Dialogs.info("PhotoPickAI Job Still Running", message)
            return
        end

        if status == "failed" or status == "interrupted" then
            if current.resumable then
                local choice = LrDialogs.confirm(
                    "PhotoPickAI Job Needs Attention",
                    errorText(current) .. "\n\nResume this job from its saved checkpoint?",
                    "Resume Job",
                    "Not Now"
                )
                if choice == "ok" then
                    local resumed, resumeExitCode, resumeError = callHelper("resume")
                    if resumeError or resumeExitCode ~= 0 then
                        Dialogs.error("PhotoPickAI Resume Error", resumeError or errorText(resumed))
                        return
                    end
                    return handleResponse(resumed)
                end
            else
                closeActiveJob("failed")
                Dialogs.error("PhotoPickAI Job Failed", errorText(current) .. "\n\nSubmit the photos again as a new job.")
            end
            return
        end

        if status ~= "completed" then
            Dialogs.warning("PhotoPickAI", "The server returned an unknown job status: " .. status)
            return
        end

        local manifestOk, manifest = pcall(function() return Json.readFile(jobJson) end)
        if not manifestOk then
            Dialogs.error("PhotoPickAI", "Could not read the original job manifest. No flags were changed.")
            return
        end
        local photoById, missing = resolvePhotos(catalog, manifest)
        if #missing > 0 then
            Dialogs.error(
                "PhotoPickAI Missing Photos",
                "Could not find " .. tostring(#missing)
                    .. " original photo(s) in this catalog. No flags were changed.\n\nPhoto IDs:\n"
                    .. table.concat(missing, "\n")
            )
            return
        end

        local valid, validationMessage = validateResult(current, photoById)
        if not valid then
            Dialogs.error("PhotoPickAI Invalid Result", validationMessage .. "\nNo flags were changed.")
            return
        end

        if not activeJob.dry_run then
            local applyProgress = LrProgressScope {
                title = "PhotoPickAI: Apply Results",
                caption = "Waiting for Lightroom catalog access...",
            }
            local appliedOk, writeStatus = LrTasks.pcall(function()
                return applyFlags(
                    catalog,
                    current,
                    photoById,
                    {
                        auto_reject_non_selected = activeJob.auto_reject_non_selected ~= false,
                    },
                    applyProgress
                )
            end)
            applyProgress:done()
            if not appliedOk then
                Dialogs.error("PhotoPickAI Apply Error", safeString(writeStatus) .. "\nNo completion marker was written.")
                return
            end
            if writeStatus ~= nil and writeStatus ~= "executed" then
                Dialogs.error(
                    "PhotoPickAI Apply Error",
                    "Lightroom catalog write access timed out. Try Check Job Status again.\n"
                        .. "No completion marker was written."
                )
                return
            end
        end

        activeJob.applied = true
        activeJob.status = "applied"
        activeJob.applied_at = os.time()
        Json.writeFile(activeJobPath, activeJob)
        Dialogs.summary(current, photoById, activeJob.dry_run == true, jobFolder)
    end

    handleResponse(response)
end)
