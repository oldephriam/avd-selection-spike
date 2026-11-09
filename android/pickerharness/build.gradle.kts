plugins {
    id("com.android.application")
}

android {
    namespace = "com.pacer.test.pickerharness"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.android.contacts"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
}

dependencies {
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
