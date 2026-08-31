// SPDX-License-Identifier: LGPL-2.1-or-later

import com.android.build.api.dsl.LibraryExtension
import java.util.Properties
import java.util.zip.ZipFile
import org.gradle.api.DefaultTask
import org.gradle.api.file.DirectoryProperty
import org.gradle.api.publish.maven.MavenPublication
import org.gradle.api.publish.maven.tasks.PublishToMavenLocal
import org.gradle.api.publish.maven.tasks.PublishToMavenRepository
import org.gradle.api.tasks.InputDirectory
import org.gradle.api.tasks.Optional
import org.gradle.api.tasks.PathSensitive
import org.gradle.api.tasks.PathSensitivity
import org.gradle.api.tasks.TaskAction
import org.gradle.api.tasks.bundling.Jar

abstract class VerifyAndroidMpvRuntimePayload : DefaultTask() {
    @get:Optional
    @get:InputDirectory
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val payload: DirectoryProperty

    @TaskAction
    fun verify() {
        if (!payload.isPresent) return
        val root = payload.get().asFile
        val expectedAbis = setOf("arm64-v8a", "armeabi-v7a")
        val expectedLibraries = setOf("libkmediampv_mpv.so", "libkmediampv_placebo.so")
        val actualAbis = root.resolve("jni").listFiles().orEmpty().filter(File::isDirectory).map(File::getName).toSet()
        require(actualAbis == expectedAbis) { "Android MPV runtime ABI set differs: $actualAbis" }
        expectedAbis.forEach { abi ->
            val libraries = root.resolve("jni/$abi").listFiles().orEmpty()
            require(libraries.map(File::getName).toSet() == expectedLibraries && libraries.all { it.length() > 0L }) {
                "Android $abi MPV runtime inventory differs."
            }
        }
        val manifest = root.resolve("manifest.properties")
        require(manifest.isFile) { "Android MPV runtime manifest is missing." }
        val values = Properties().apply { manifest.inputStream().use(::load) }
        require(values.getProperty("licenseSpdx") == "LGPL-2.1-or-later") {
            "Android MPV runtime manifest does not declare LGPL-2.1-or-later."
        }
    }
}

plugins {
    alias(libs.plugins.android.library)
    `maven-publish`
}

val ffmpegRuntimeVersion =
    providers.gradleProperty("kmediaFfmpegRuntimeVersion").orElse("0.1.0-SNAPSHOT").get()
val nativePayload = providers.gradleProperty("kmediaMpvRuntimeAndroidPayloadDirectory").map(rootProject::file)
val correspondingSourceArchive = providers.gradleProperty("correspondingSourceArchive").map(rootProject::file)
val publicationVersionValue = project.version.toString()
val generatedAssets = layout.buildDirectory.dir("generated/runtimeAssets")

val emptyJavadocJar =
    tasks.register<Jar>("emptyJavadocJar") {
        archiveClassifier.set("javadoc")
    }

extensions.configure<LibraryExtension> {
    namespace = "cc.suviomedia.kmediampv.runtime.android"
    compileSdk = 37
    enableKotlin = false
    defaultConfig { minSdk = 28 }
    sourceSets.named("main") {
        jniLibs.directories.add((nativePayload.orNull?.resolve("jni") ?: layout.buildDirectory.dir("empty-jni").get().asFile).absolutePath)
        assets.directories.add(generatedAssets.get().asFile.absolutePath)
    }
    publishing { singleVariant("release") { withSourcesJar() } }
}

dependencies {
    api("cc.suviomedia:kmedia-ffmpeg-runtime-android:$ffmpegRuntimeVersion") {
        version { strictly(ffmpegRuntimeVersion) }
    }
}

val prepareRuntimeAssets =
    tasks.register<Sync>("prepareRuntimeAssets") {
        into(generatedAssets.map { it.dir("kmediampv/legal") })
        from(rootProject.layout.projectDirectory.file("LICENSE"))
        from(rootProject.layout.projectDirectory.file("NOTICE"))
        from(rootProject.layout.projectDirectory.file("THIRD_PARTY_NOTICES.md"))
        from(rootProject.layout.projectDirectory.dir("LICENSES")) { into("LICENSES") }
        nativePayload.orNull?.let { from(it.resolve("manifest.properties")) }
    }
val verifyNativePayload =
    tasks.register<VerifyAndroidMpvRuntimePayload>("verifyNativePayload") {
        payload.set(layout.dir(nativePayload))
    }
tasks.named("preBuild") { dependsOn(prepareRuntimeAssets, verifyNativePayload) }
tasks.withType<PublishToMavenRepository>().configureEach {
    dependsOn(verifyNativePayload)
    dependsOn(emptyJavadocJar)
    doFirst {
        require(nativePayload.isPresent) { "Publishing requires -PkmediaMpvRuntimeAndroidPayloadDirectory." }
        require(correspondingSourceArchive.isPresent) { "Publishing requires -PcorrespondingSourceArchive." }
    }
}
tasks.withType<PublishToMavenLocal>().configureEach {
    dependsOn(verifyNativePayload)
    dependsOn(emptyJavadocJar)
    doFirst {
        require(nativePayload.isPresent) { "Publishing requires -PkmediaMpvRuntimeAndroidPayloadDirectory." }
        require(correspondingSourceArchive.isPresent) { "Publishing requires -PcorrespondingSourceArchive." }
    }
}
tasks.withType<Jar>().matching { it.name.contains("sources", ignoreCase = true) }.configureEach {
    from(rootProject.layout.projectDirectory.dir("native")) { into("native") }
    from(rootProject.layout.projectDirectory.dir("scripts")) { into("scripts") }
    from(rootProject.layout.projectDirectory.dir("compliance")) { into("compliance") }
}

afterEvaluate {
    publishing {
        publications {
            create<MavenPublication>("release") {
                from(components["release"])
                groupId = "cc.suviomedia"
                artifactId = "kmedia-mpv-lgpl-runtime-android"
                version = publicationVersionValue
                artifact(emptyJavadocJar)
                correspondingSourceArchive.orNull?.let { source ->
                    artifact(source) {
                        classifier = "corresponding-source"
                        extension = "tar.gz"
                    }
                }
                pom {
                    name.set("KMediaMpv LGPL Runtime for Android")
                    description.set("Replaceable LGPL mpv/libplacebo runtime for Android.")
                    inceptionYear.set("2026")
                    url.set("https://github.com/SuvioMedia/KMediaMpvRuntime")
                    licenses {
                        license {
                            name.set("GNU Lesser General Public License, version 2.1 or later")
                            url.set("https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html")
                            distribution.set("repo")
                        }
                    }
                    developers { developer { id.set("SuvioMedia"); name.set("SuvioMedia") } }
                    scm {
                        connection.set("scm:git:https://github.com/SuvioMedia/KMediaMpvRuntime.git")
                        developerConnection.set("scm:git:ssh://git@github.com/SuvioMedia/KMediaMpvRuntime.git")
                        url.set("https://github.com/SuvioMedia/KMediaMpvRuntime")
                    }
                }
            }
        }
    }
}

publishing.repositories {
    rootProject.providers.gradleProperty("releaseRepository").orNull?.let { repositoryPath ->
        maven { name = "release"; url = uri(repositoryPath) }
    }
}
