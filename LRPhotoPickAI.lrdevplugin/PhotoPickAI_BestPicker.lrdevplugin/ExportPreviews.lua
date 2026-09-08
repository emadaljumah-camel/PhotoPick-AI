local LrExportSession = import "LrExportSession"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"

local ExportPreviews = {}

local function getRawMetadata(photo, key)
    return photo:getRawMetadata(key)
end

local function getFormattedMetadata(photo, key)
    return photo:getFormattedMetadata(key)
end

local function getOriginalFilename(photo)
    local path = getRawMetadata(photo, "path")
    if path and path ~= "" then
        return LrPathUtils.leafName(path)
    end
    local rawFilename = getRawMetadata(photo, "fileName")
    if rawFilename and rawFilename ~= "" then
        return tostring(rawFilename)
    end
    return tostring(getFormattedMetadata(photo, "fileName") or "")
end

local function sanitizedPreviewStem(filename)
    local stem = filename:gsub("%.[^.]*$", "")
    stem = stem:gsub('[<>:"/\\|%?%*]', "_")
    stem = stem:gsub("[%c]", "_")
    stem = stem:gsub("^%s+", ""):gsub("%s+$", "")
    stem = stem:gsub("[%.%s]+$", "")
    if stem == "" then
        return "photo"
    end
    return stem
end

local function uniquePreviewName(stem, usedNames)
    local previewName = stem .. ".jpg"
    local suffix = 2
    while usedNames[previewName] do
        previewName = string.format("%s_%04d.jpg", stem, suffix)
        suffix = suffix + 1
    end
    usedNames[previewName] = true
    return previewName
end

function ExportPreviews.export(photos, previewFolder, settings, logger, progressScope)
    LrFileUtils.createAllDirectories(previewFolder)
    local exported = {}
    local skipped = {}
    local usedPreviewNames = {}

    local exportSettings = {
        LR_export_destinationType = "specificFolder",
        LR_export_destinationPathPrefix = previewFolder,
        LR_export_useSubfolder = false,
        LR_format = "JPEG",
        LR_jpeg_quality = settings.jpeg_quality / 100,
        LR_size_doConstrain = true,
        LR_size_maxHeight = settings.preview_long_edge,
        LR_size_maxWidth = settings.preview_long_edge,
        LR_size_resizeType = "longEdge",
        LR_outputSharpeningOn = false,
        LR_minimizeEmbeddedMetadata = false,
        LR_collisionHandling = "overwrite",
    }

    local exportSession = LrExportSession {
        photosToExport = photos,
        exportSettings = exportSettings,
    }

    for index, rendition in exportSession:renditions { stopIfCanceled = true } do
        if progressScope then
            progressScope:setCaption(string.format("Exporting preview %d of %d...", index, #photos))
            progressScope:setPortionComplete(index - 1, math.max(#photos, 1))
        end
        local photo = rendition.photo
        local photoId = tostring(photo.localIdentifier or photo:getRawMetadata("uuid") or ("photo_" .. string.format("%04d", index)))
        local success, pathOrMessage = rendition:waitForRender()
        if success then
            local originalFilename = getOriginalFilename(photo)
            if originalFilename == "" then
                originalFilename = LrPathUtils.leafName(pathOrMessage)
            end
            local previewStem = sanitizedPreviewStem(originalFilename)
            if previewStem == "photo" then
                previewStem = string.format("photo_%04d_%s", index, photoId:gsub("[^%w_%-]", "_"))
            end
            local previewName = uniquePreviewName(previewStem, usedPreviewNames)
            local finalPath = LrPathUtils.child(previewFolder, previewName)
            LrFileUtils.move(pathOrMessage, finalPath)
            exported[photo] = {
                photo_id = photoId,
                original_filename = originalFilename,
                preview_filename = previewName,
                preview_path = finalPath,
            }
        else
            table.insert(skipped, { photo_id = photoId, error = tostring(pathOrMessage) })
            logger.log("Preview export failed for " .. photoId .. ": " .. tostring(pathOrMessage))
        end
    end

    if progressScope then
        progressScope:setCaption("Finished exporting previews.")
        progressScope:setPortionComplete(#photos, math.max(#photos, 1))
    end
    logger.log(
        "Preview export complete exported=" .. tostring(#photos - #skipped)
            .. " skipped=" .. tostring(#skipped)
            .. " folder=" .. previewFolder
    )

    return exported, skipped
end

return ExportPreviews
