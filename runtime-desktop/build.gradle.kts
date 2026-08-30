// SPDX-License-Identifier: LGPL-2.1-or-later

import java.security.MessageDigest
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
import org.gradle.jvm.tasks.Jar

abstract class VerifyDesktopMpvRuntimePayload : DefaultTask() {
    @get:Optional
    @get:InputDirectory
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val payload: DirectoryProperty

    @TaskAction
    fun verify() {
        if (!payload.isPresent) return
        val nativeRoot = payload.get().asFile.resolve("META-INF/kmediampv/native")
        val expectedPlatforms = setOf("linux-x86_64", "linux-aarch64", "macos-aarch64", "windows-x86_64")
        val actualPlatforms = nativeRoot.listFiles().orEmpty().filter(File::isDirectory).map(File::getName).toSet()
        require(actualPlatforms == expectedPlatforms) { "Desktop MPV runtime matrix differs: $actualPlatforms" }
        expectedPlatforms.forEach { platform ->
            val directory = nativeRoot.resolve(platform)
            val manifestFile = directory.resolve("manifest.properties")
            require(manifestFile.isFile) { "Missing MPV runtime manifest for $platform." }
            val manifest = Properties().apply { manifestFile.inputStream().use(::load) }
            require(manifest.getProperty("licenseSpdx") == "LGPL-2.1-or-later") {
                "MPV runtime manifest does not declare LGPL for $platform."
            }
            val count = manifest.getProperty("library.count").toInt()
            val names = (0 until count).map { index -> manifest.getProperty("library.$index.name") }
            require(names.none { name -> name.contains("jni", ignoreCase = true) }) {
                "The public MPV runtime contains a private JNI library."
            }
            names.forEachIndexed { index, name ->
                val library = directory.resolve(name)
                require(library.isFile && library.length() > 0L) { "Missing MPV runtime library $name." }
                val sha256 =
                    MessageDigest.getInstance("SHA-256").digest(library.readBytes()).joinToString("") { byte ->
                        "%02x".format(byte.toInt() and 0xff)
                    }
                require(sha256 == manifest.getProperty("library.$index.sha256")) {
                    "MPV runtime checksum differs for $platform/$name."
                }
            }
        }
    }
}

plugins {
    `java-library`
    `maven-publish`
}

val ffmpegRuntimeVersion =
    providers.gradleProperty("kmediaFfmpegRuntimeVersion").orElse("0.1.0-SNAPSHOT").get()
val nativePayload = providers.gradleProperty("kmediaMpvRuntimeDesktopPayloadDirectory").map(rootProject::file)
val publicationVersionValue = project.version.toString()

java {
    toolchain.languageVersion.set(JavaLanguageVersion.of(25))
    withSourcesJar()
    withJavadocJar()
}

sourceSets.main {
    nativePayload.orNull?.let { resources.srcDir(it) }
}

dependencies {
    api("cc.suviomedia:kmedia-ffmpeg-runtime-desktop:$ffmpegRuntimeVersion") {
        version { strictly(ffmpegRuntimeVersion) }
    }
}

tasks.withType<Jar>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
}
tasks.named<ProcessResources>("processResources") {
    from(rootProject.layout.projectDirectory.file("LICENSE")) { into("META-INF") }
    from(rootProject.layout.projectDirectory.file("NOTICE")) { into("META-INF") }
    from(rootProject.layout.projectDirectory.file("THIRD_PARTY_NOTICES.md")) { into("META-INF") }
    from(rootProject.layout.projectDirectory.dir("LICENSES")) { into("META-INF/LICENSES") }
}
tasks.named<Jar>("sourcesJar") {
    from(rootProject.layout.projectDirectory.dir("native")) { into("native") }
    from(rootProject.layout.projectDirectory.dir("scripts")) { into("scripts") }
    from(rootProject.layout.projectDirectory.dir("compliance")) { into("compliance") }
}
val verifyNativePayload =
    tasks.register<VerifyDesktopMpvRuntimePayload>("verifyNativePayload") {
        payload.set(layout.dir(nativePayload))
    }
tasks.named("check") { dependsOn(verifyNativePayload) }
tasks.withType<PublishToMavenRepository>().configureEach {
    dependsOn(verifyNativePayload)
    doFirst { require(nativePayload.isPresent) { "Publishing requires -PkmediaMpvRuntimeDesktopPayloadDirectory." } }
}
tasks.withType<PublishToMavenLocal>().configureEach {
    dependsOn(verifyNativePayload)
    doFirst { require(nativePayload.isPresent) { "Publishing requires -PkmediaMpvRuntimeDesktopPayloadDirectory." } }
}

publishing {
    publications {
        create<MavenPublication>("maven") {
            from(components["java"])
            groupId = "cc.suviomedia"
            artifactId = "kmedia-mpv-lgpl-runtime-desktop"
            version = publicationVersionValue
            pom {
                name.set("KMediaMpv LGPL Runtime for Desktop")
                description.set("Replaceable LGPL mpv/libplacebo runtime for desktop platforms.")
                url.set("https://github.com/SuvioMedia/KMediaMpvRuntime")
                licenses {
                    license {
                        name.set("GNU Lesser General Public License, version 2.1 or later")
                        url.set("https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html")
                        distribution.set("repo")
                    }
                }
                developers { developer { id.set("SuvioMedia"); name.set("SuvioMedia") } }
                scm { url.set("https://github.com/SuvioMedia/KMediaMpvRuntime") }
            }
        }
    }
    repositories {
        rootProject.providers.gradleProperty("releaseRepository").orNull?.let { repositoryPath ->
            maven { name = "release"; url = uri(repositoryPath) }
        }
    }
}
