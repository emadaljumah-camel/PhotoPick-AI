return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = "com.photopickai.best-similar-picker",
    LrPluginName = "03 PhotoPickAI Best Similar Picker",
    LrInitPlugin = "Settings.lua",
    LrPluginInfoProvider = "PluginInfoProvider.lua",
    LrLibraryMenuItems = {
        {
            title = "PhotoPickAI: Submit Selected Photos",
            file = "PickBestSimilar.lua",
        },
        {
            title = "PhotoPickAI: Check Job Status / Apply Results",
            file = "CheckJobStatus.lua",
        },
    },
    VERSION = {
        major = 1,
        minor = 1,
        revision = 0,
        build = 0,
    },
}
