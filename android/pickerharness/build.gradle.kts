plugins {
    id("com.android.application")
}

android {
    namespace = "com.pacer.test.pickerharness"
    compileSdk = 35
    useLibrary("android.test.base")
    useLibrary("android.test.runner")

    defaultConfig {
        applicationId = "com.pacer.test.pickerharness"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }
}

dependencies {
    compileOnly(files(
        "${android.sdkDirectory}/platforms/android-35/optional/android.test.base.jar",
        "${android.sdkDirectory}/platforms/android-35/optional/android.test.runner.jar",
    ))
}
