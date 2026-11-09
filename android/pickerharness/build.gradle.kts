plugins {
    id("com.android.application")
}

android {
    namespace = "com.pacer.test.pickerharness"
    compileSdk = 35
    useLibrary("android.test.runner")

    defaultConfig {
        applicationId = "com.pacer.test.pickerharness"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }
}
