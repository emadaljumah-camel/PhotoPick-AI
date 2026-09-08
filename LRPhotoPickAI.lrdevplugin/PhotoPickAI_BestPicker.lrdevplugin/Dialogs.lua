local LrDialogs = import "LrDialogs"

local Dialogs = {}

local function displayName(photoId, entry)
    if entry then
        if entry.filename and entry.filename ~= "" then
            return entry.filename
        end
        if entry.preview_filename and entry.preview_filename ~= "" then
            return entry.preview_filename
        end
    end
    return tostring(photoId)
end

local function addIds(target, ids, seen)
    for _, photoId in ipairs(ids or {}) do
        if not seen[photoId] then
            table.insert(target, photoId)
            seen[photoId] = true
        end
    end
end

local function groupSummary(group, photoById, maxPhotos)
    local lines = {}
    local groupName = group.group_id or "group"
    local allPhotoIds = {}
    local seen = {}
    addIds(allPhotoIds, group.photos, seen)
    addIds(allPhotoIds, group.representatives, seen)
    addIds(allPhotoIds, group.rejected, seen)

    local pickedNames = {}
    for _, photoId in ipairs(group.representatives or {}) do
        table.insert(pickedNames, displayName(photoId, photoById[photoId]))
    end

    table.insert(lines, groupName)
    table.insert(lines, "Picked: " .. table.concat(pickedNames, ", "))
    table.insert(lines, "Photos:")
    local shown = math.min(#allPhotoIds, maxPhotos or #allPhotoIds)
    for index = 1, shown do
        local photoId = allPhotoIds[index]
        table.insert(lines, "  " .. displayName(photoId, photoById[photoId]))
    end
    if shown < #allPhotoIds then
        table.insert(lines, "  ... " .. tostring(#allPhotoIds - shown) .. " more photo(s)")
    end
    return table.concat(lines, "\n")
end

function Dialogs.info(title, message)
    LrDialogs.message(title, message, "info")
end

function Dialogs.warning(title, message)
    LrDialogs.message(title, message, "warning")
end

function Dialogs.error(title, message)
    LrDialogs.message(title, message, "critical")
end

function Dialogs.summary(result, photoById, dryRun, jobFolder)
    local picked = {}
    local rejected = {}
    local lines = {}
    local groups = result.groups or {}
    local totalPhotos = 0
    for _, group in ipairs(groups) do
        totalPhotos = totalPhotos + #(group.photos or {})
    end
    local compact = #groups > 50 or totalPhotos > 200
    local groupsToShow = compact and math.min(#groups, 20) or #groups
    for index, group in ipairs(groups) do
        if index <= groupsToShow then
            table.insert(lines, groupSummary(group, photoById, compact and 20 or nil))
        end
        for _, photoId in ipairs(group.representatives or {}) do
            local entry = photoById[photoId]
            table.insert(picked, displayName(photoId, entry))
        end
        for _, photoId in ipairs(group.rejected or {}) do
            local entry = photoById[photoId]
            table.insert(rejected, displayName(photoId, entry))
        end
    end

    local title = dryRun and "PhotoPickAI Dry Run - No Flags Changed" or "PhotoPickAI Finished"
    local pickedLabel = dryRun and "Proposed picked" or "Picked flags applied"
    local rejectedLabel = dryRun and "Proposed rejected" or "Rejected flags applied"
    if compact then
        table.insert(lines, "... " .. tostring(#groups - groupsToShow) .. " more group(s) in the saved result")
    end
    local reportPath = jobFolder and (jobFolder .. "\\completed_result.json") or ""
    local body = string.format(
        "Groups: %d\n%s: %d\n%s: %d\nSkipped: %d\nErrors: %d%s\n\nGroups:\n%s",
        #groups,
        pickedLabel,
        #picked,
        rejectedLabel,
        #rejected,
        #(result.skipped or {}),
        #(result.errors or {}),
        reportPath ~= "" and ("\nFull result: " .. reportPath) or "",
        table.concat(lines, "\n\n")
    )
    LrDialogs.message(title, body, "info")
end

return Dialogs
