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

local function groupSummary(group, photoById)
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
    for _, photoId in ipairs(allPhotoIds) do
        table.insert(lines, "  " .. displayName(photoId, photoById[photoId]))
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

function Dialogs.summary(result, photoById, dryRun)
    local picked = {}
    local rejected = {}
    local lines = {}
    for _, group in ipairs(result.groups or {}) do
        table.insert(lines, groupSummary(group, photoById))
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
    local body = string.format(
        "Groups: %d\n%s: %d\n%s: %d\nSkipped: %d\nErrors: %d\n\nGroups:\n%s",
        #(result.groups or {}),
        pickedLabel,
        #picked,
        rejectedLabel,
        #rejected,
        #(result.skipped or {}),
        #(result.errors or {}),
        table.concat(lines, "\n\n")
    )
    LrDialogs.message(title, body, "info")
end

return Dialogs
