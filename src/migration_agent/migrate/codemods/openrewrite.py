"""OpenRewrite adapter: Java 8 -> 17/21 and Spring Boot 2 -> 3 recipes (Maven projects), run in the sandbox.

Recipe artifacts are resolved through the package mirror during the build phase (network `mirror`). Versions are
pinned here; bump them deliberately. Gradle projects are skipped with a reason for now (they need the rewrite
Gradle plugin or an init script; TODO).
"""
from __future__ import annotations

import re

PLUGIN = "org.openrewrite.maven:rewrite-maven-plugin:5.46.1"
RECIPE_ARTIFACTS = ("org.openrewrite.recipe:rewrite-migrate-java:2.31.0,"
                    "org.openrewrite.recipe:rewrite-spring:5.25.0")
JAVA_RECIPES = {"17": "org.openrewrite.java.migrate.UpgradeToJava17",
                "21": "org.openrewrite.java.migrate.UpgradeToJava21"}
BOOT3_RECIPE = "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0"


def _major(text: str) -> str:
    m = re.search(r"(\d+)", text or "")
    return m.group(1) if m else ""


class OpenRewriteAdapter:
    name = "openrewrite"
    image = "maven:3.9-eclipse-temurin-21"

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

    def command(self, spec) -> list[str] | str:
        build = (spec.target.build or "maven").lower()
        if build != "maven":
            return f"openrewrite: build tool {build!r} not supported yet (Maven only; TODO Gradle init script)"
        return ["mvn", "-B", "-q", "--no-transfer-progress", f"{PLUGIN}:run",
                f"-Drewrite.recipeArtifactCoordinates={RECIPE_ARTIFACTS}",
                f"-Drewrite.activeRecipes={','.join(self.recipes(spec))}", "-Drewrite.exportDatatables=false"]
