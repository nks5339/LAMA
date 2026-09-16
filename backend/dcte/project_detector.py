"""Auto-detect legacy project type from a folder path."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


HELIDON_MP_MARKERS = (
    "microprofile-config.properties",
    "META-INF/microprofile-config.properties",
)
HELIDON_MP_POM_TOKENS = ("io.helidon.microprofile", "helidon-microprofile-bundle")
HELIDON_SE_POM_TOKENS = ("io.helidon.webserver", "helidon-webserver", "io.helidon.se")
SPRING_POM_TOKENS = ("org.springframework.boot", "spring-boot-starter")

ORACLE_SQL_MARKERS = (
    "VARCHAR2", "NUMBER(", "SYSDATE", "NVL(", "DECODE(", "CREATE OR REPLACE PACKAGE",
    "DUAL", "ROWNUM", "MINUS",
)


@dataclass
class ProjectFingerprint:
    path: str
    exists: bool
    is_valid: bool
    detected_stack: str = "unknown"
    confidence: float = 0.0
    hints: list[str] = field(default_factory=list)
    suggested_target: str | None = None


class ProjectDetector:
    def detect(self, source_path: str) -> ProjectFingerprint:
        p = Path(source_path).expanduser()
        fp = ProjectFingerprint(path=str(p), exists=p.exists(), is_valid=False)
        if not p.exists() or not p.is_dir():
            fp.hints.append("Path does not exist or is not a directory.")
            return fp

        pom = p / "pom.xml"
        gradle = p / "build.gradle"
        gradle_kts = p / "build.gradle.kts"
        has_java = any(p.rglob("*.java"))
        sql_files = list(p.rglob("*.sql"))

        if pom.exists():
            try:
                content = pom.read_text(encoding="utf-8", errors="ignore")
                if any(tok in content for tok in HELIDON_MP_POM_TOKENS):
                    fp.detected_stack = "helidon-mp"
                    fp.confidence = 0.92
                    fp.suggested_target = "spring-boot-3"
                    fp.hints.append("pom.xml declares Helidon MicroProfile dependencies.")
                    fp.is_valid = True
                    return fp
                if any(tok in content for tok in HELIDON_SE_POM_TOKENS):
                    fp.detected_stack = "helidon-se"
                    fp.confidence = 0.88
                    fp.suggested_target = "spring-boot-3"
                    fp.hints.append("pom.xml declares Helidon SE dependencies.")
                    fp.is_valid = True
                    return fp
                if any(tok in content for tok in SPRING_POM_TOKENS):
                    fp.detected_stack = "spring-boot"
                    fp.confidence = 0.9
                    fp.is_valid = True
                    fp.hints.append("Already Spring Boot — no application-tier transform needed.")
                    return fp
            except Exception as e:
                fp.hints.append(f"Could not parse pom.xml: {e}")

        if gradle.exists() or gradle_kts.exists():
            fp.hints.append("Gradle build detected — Helidon/Spring inference requires pom.xml parity.")
            fp.detected_stack = "java-gradle"
            fp.confidence = 0.4
            fp.is_valid = has_java

        for marker in HELIDON_MP_MARKERS:
            if (p / marker).exists() or any(p.rglob(marker)):
                fp.detected_stack = "helidon-mp"
                fp.confidence = max(fp.confidence, 0.75)
                fp.suggested_target = "spring-boot-3"
                fp.hints.append(f"Found MicroProfile config marker: {marker}")
                fp.is_valid = True
                return fp

        if sql_files:
            hits = 0
            for f in sql_files[:20]:
                try:
                    txt = f.read_text(encoding="utf-8", errors="ignore").upper()
                    if any(tok in txt for tok in ORACLE_SQL_MARKERS):
                        hits += 1
                except Exception:
                    continue
            if hits > 0:
                fp.detected_stack = "oracle"
                fp.confidence = min(1.0, 0.5 + 0.1 * hits)
                fp.suggested_target = "postgres-15"
                fp.hints.append(f"Detected Oracle SQL markers in {hits} file(s).")
                fp.is_valid = True
                return fp

        if has_java:
            fp.detected_stack = "java-generic"
            fp.confidence = 0.35
            fp.is_valid = True
            fp.hints.append("Generic Java project — no Helidon markers found.")
            return fp

        fp.hints.append("No recognisable stack markers found.")
        return fp
