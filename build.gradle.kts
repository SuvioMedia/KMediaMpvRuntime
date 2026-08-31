// SPDX-License-Identifier: LGPL-2.1-or-later

plugins {
    base
    alias(libs.plugins.android.library) apply false
}

val publicationVersion = providers.gradleProperty("publicationVersion").orElse("1.0.0-SNAPSHOT")

allprojects {
    group = "cc.suviomedia"
    version = publicationVersion.get()
}

val verifyPublicBoundary =
    tasks.register("verifyPublicBoundary") {
        group = "verification"
        description = "Rejects proprietary MPV client code from the public LGPL runtime."
        notCompatibleWithConfigurationCache("Reads the complete public runtime source boundary.")
        val publicFiles =
            fileTree(layout.projectDirectory) {
                exclude(".git/**", ".gradle/**", "**/build/**", "docs/**")
            }
        inputs.files(publicFiles)
        inputs.files("LICENSE", "LICENSES/LGPL-2.1.txt", "NOTICE")

        doLast {
            val forbiddenRoots =
                listOf("client-android", "client-desktop", "runtime-android/src/main/java", "runtime-desktop/src/main/java")
                    .filter { path -> rootProject.file(path).exists() }
            require(forbiddenRoots.isEmpty()) {
                "Private MPV client roots remain public: $forbiddenRoots"
            }
            val proprietaryNativeFiles =
                fileTree("native") {
                    include("**/*.c", "**/*.h", "**/CMakeLists.txt")
                }.files.filter { file ->
                    "LicenseRef-KMediaMpv-Proprietary" in file.readText().take(512)
                }
            require(proprietaryNativeFiles.isEmpty()) {
                "Proprietary native client source remains public: ${proprietaryNativeFiles.sorted()}"
            }
            require(!rootProject.file("native/android/jni").exists()) {
                "The proprietary Android JNI bridge must remain in KMediaMpvClient."
            }
        }
    }

tasks.named("check") {
    dependsOn(
        verifyPublicBoundary,
        ":kmedia-mpv-lgpl-runtime-android:check",
        ":kmedia-mpv-lgpl-runtime-desktop:check",
    )
}

tasks.register("verifyAll") {
    group = "verification"
    dependsOn("check")
}
