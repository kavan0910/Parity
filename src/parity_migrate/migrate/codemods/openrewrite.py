"""OpenRewrite adapter: Java 8 -> 17/21 and Spring Boot 2 -> 3 recipes (Maven projects), run in the sandbox.

Recipe artifacts are resolved through the package mirror during the build phase (network `mirror`). Versions are
pinned here; bump them deliberately. Maven projects use the rewrite-maven-plugin; Gradle projects use the
rewrite-gradle-plugin applied through a generated init script (the build file is never edited), run in a Gradle
image (`image_for`). Neither route has run against a real daemon yet.
"""
from __future__ import annotations

import re

PLUGIN = "org.openrewrite.maven:rewrite-maven-plugin:5.46.1"
RECIPE_ARTIFACTS = ("org.openrewrite.recipe:rewrite-migrate-java:2.31.0,"
                    "org.openrewrite.recipe:rewrite-spring:5.25.0")
JAVA_RECIPES = {"17": "org.openrewrite.java.migrate.UpgradeToJava17",
                "21": "org.openrewrite.java.migrate.UpgradeToJava21"}
BOOT3_RECIPE = "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0"
MAVEN_SETTINGS = (
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"><proxies>'
    '<proxy><id>ma-https</id><active>true</active><protocol>https</protocol><host>egress-proxy</host><port>3128</port></proxy>'
    '<proxy><id>ma-http</id><active>true</active><protocol>http</protocol><host>egress-proxy</host><port>3128</port></proxy>'
    '</proxies></settings>\n')
GRADLE_PLUGIN = "org.openrewrite:plugin:6.28.0"
GRADLE_IMAGE = "gradle:8.10-jdk21"
_GRADLE_RECIPE_DEPS = ("org.openrewrite.recipe:rewrite-migrate-java:2.31.0",
                       "org.openrewrite.recipe:rewrite-spring:5.25.0")


def gradle_init_script() -> str:
    deps = "\n".join(f'        rewrite("{d}")' for d in _GRADLE_RECIPE_DEPS)
    return (f"initscript {{\n    repositories {{ gradlePluginPortal() }}\n"
            f'    dependencies {{ classpath("{GRADLE_PLUGIN}") }}\n}}\n'
            "rootProject {\n    plugins.apply(org.openrewrite.gradle.RewritePlugin)\n"
            f"    dependencies {{\n{deps}\n    }}\n"
            "    repositories { mavenCentral() }\n}\n")


def _major(text: str) -> str:
    m = re.search(r"(\d+)", text or "")
    return m.group(1) if m else ""


class OpenRewriteAdapter:
    name = "openrewrite"
    image = "maven:3.9-eclipse-temurin-21"
    cache = "harness/cache/m2"            # mounted at /cache: Maven's local repository and Gradle's user home

    def recipes(self, spec) -> list[str]:
        out = []
        jv = _major(spec.target.runtime) if "java" in (spec.target.runtime or "").lower() or \
            spec.target.language.lower() == "java" else ""
        if jv in JAVA_RECIPES and _major(spec.source.runtime) != jv:
            out.append(JAVA_RECIPES[jv])
        tf, sf = (spec.target.framework or "").lower(), (spec.source.framework or "").lower()
        if "spring-boot" in tf and re.search(r"@3", tf) and not re.search(r"@3", sf):
            out.append(BOOT3_RECIPE)
        return out

    def applies(self, spec) -> tuple[bool, str]:
        if spec.source.language.lower() != "java" or spec.target.language.lower() != "java":
            return False, "not a Java -> Java migration"
        if not self.recipes(spec):
            return False, "no Java 17/21 or Spring Boot 3 upgrade in target.yaml"
        return True, ""

    def image_for(self, spec) -> str:
        return GRADLE_IMAGE if (spec.target.build or "maven").lower() == "gradle" else self.image

    def command(self, spec) -> list[str] | str:
        build = (spec.target.build or "maven").lower()
        if build == "gradle":
            import shlex
            script = ("export HOME=/tmp GRADLE_USER_HOME=/cache/gradle && "
                      "cat > /tmp/rewrite.init.gradle <<'EOF'\n" + gradle_init_script() + "EOF\n"
                      "(gradle --no-daemon --console=plain -q --init-script /tmp/rewrite.init.gradle rewriteRun "
                      f"-Drewrite.activeRecipes={shlex.quote(','.join(self.recipes(spec)))} "
                      "|| ./gradlew --no-daemon --console=plain -q --init-script /tmp/rewrite.init.gradle "
                      f"rewriteRun -Drewrite.activeRecipes={shlex.quote(','.join(self.recipes(spec)))})")
            return ["sh", "-c", script]
        if build != "maven":
            return f"openrewrite: build tool {build!r} not supported (Maven and Gradle only)"
        import shlex
        args = ["mvn", "-B", "-q", "--no-transfer-progress", "-s", "/tmp/settings.xml", "-Dmaven.repo.local=/cache/m2",
                f"{PLUGIN}:run", f"-Drewrite.recipeArtifactCoordinates={RECIPE_ARTIFACTS}",
                f"-Drewrite.activeRecipes={','.join(self.recipes(spec))}", "-Drewrite.exportDatatables=false"]
        # Maven ignores HTTPS_PROXY: the sandbox egress proxy is configured through a generated settings file
        # (no credentials, no repositories: artifacts come from Maven Central through the allowlist).
        # OpenRewrite resolves POMs with its own HTTP client, which reads ~/.m2/settings.xml (HOME=/tmp) and the
        # JVM proxy properties, not the -s file: provide all three.
        jvm = ("-Duser.home=/tmp -Dhttps.proxyHost=egress-proxy -Dhttps.proxyPort=3128 "
               "-Dhttp.proxyHost=egress-proxy -Dhttp.proxyPort=3128")
        return ["sh", "-c", f"export HOME=/tmp MAVEN_OPTS='{jvm}' && mkdir -p /tmp/.m2 && "
                "cat > /tmp/settings.xml <<'EOF'\n" + MAVEN_SETTINGS + "EOF\n"
                "cp /tmp/settings.xml /tmp/.m2/settings.xml && " + shlex.join(args)]
