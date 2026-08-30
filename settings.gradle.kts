// SPDX-License-Identifier: LGPL-2.1-or-later

pluginManagement {
    repositories {
        google()
        gradlePluginPortal()
        mavenCentral()
    }
}

plugins {
    id("org.gradle.toolchains.foojay-resolver-convention") version "1.0.0"
}

rootProject.name = "KMediaMpvRuntime"

dependencyResolutionManagement {
    repositories {
        providers.gradleProperty("kmediaFfmpegRuntimeRepository").orNull?.let { repositoryPath ->
            maven { url = uri(repositoryPath) }
        }
        google()
        mavenCentral()
    }
}

providers.gradleProperty("kmediaFfmpegRuntimeProjectDir").orNull?.let { runtimeDirectory ->
    includeBuild(runtimeDirectory) {
        dependencySubstitution {
            substitute(module("cc.suviomedia:kmedia-ffmpeg-runtime-android"))
                .using(project(":kmedia-ffmpeg-runtime-android"))
            substitute(module("cc.suviomedia:kmedia-ffmpeg-runtime-desktop"))
                .using(project(":kmedia-ffmpeg-runtime-desktop"))
        }
    }
}

include(":kmedia-mpv-lgpl-runtime-android")
project(":kmedia-mpv-lgpl-runtime-android").projectDir = file("runtime-android")
include(":kmedia-mpv-lgpl-runtime-desktop")
project(":kmedia-mpv-lgpl-runtime-desktop").projectDir = file("runtime-desktop")
