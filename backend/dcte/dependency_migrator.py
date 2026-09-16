"""pom.xml → Spring Boot 3 / Java 21 dependency migrator."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any

SPRING_BOOT_VERSION = "3.3.4"
JAVA_VERSION = "21"

_SPRING_POM = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
                             http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{{group_id}}</groupId>
  <artifactId>{{artifact_id}}</artifactId>
  <version>{{version}}</version>
  <packaging>jar</packaging>

  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{SPRING_BOOT_VERSION}</version>
    <relativePath/>
  </parent>

  <properties>
    <java.version>{JAVA_VERSION}</java.version>
    <maven.compiler.source>{JAVA_VERSION}</maven.compiler.source>
    <maven.compiler.target>{JAVA_VERSION}</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-actuator</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-security</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-validation</artifactId>
    </dependency>
    <dependency>
      <groupId>io.micrometer</groupId>
      <artifactId>micrometer-registry-prometheus</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springdoc</groupId>
      <artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>
      <version>2.6.0</version>
    </dependency>
    <dependency>
      <groupId>org.postgresql</groupId>
      <artifactId>postgresql</artifactId>
      <scope>runtime</scope>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-core</artifactId>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-database-postgresql</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""


class DependencyMigrator:
    def migrate_pom(self, source_pom: Path, target_pom: Path) -> dict[str, Any]:
        original = source_pom.read_text(encoding="utf-8", errors="ignore")
        # iter-18.4 — extract project-level GAV, NOT the <parent> GAV.
        # Strip out the <parent>…</parent> block before regex extraction, else
        # Helidon poms hand us `io.helidon.applications` / `helidon-mp` which is
        # the parent's identity, not the module being migrated.
        stripped = re.sub(r"<parent>.*?</parent>", "", original, flags=re.DOTALL)
        group_id = _extract(stripped, r"<groupId>([^<]+)</groupId>", "com.example")
        artifact_id = _extract(stripped, r"<artifactId>([^<]+)</artifactId>", "app")
        version = _extract(stripped, r"<version>([^<]+)</version>", "1.0.0")

        rendered = _SPRING_POM.format(
            group_id=group_id, artifact_id=artifact_id, version=version,
        )
        target_pom.parent.mkdir(parents=True, exist_ok=True)
        target_pom.write_text(rendered, encoding="utf-8")
        return {
            "group_id": group_id,
            "artifact_id": artifact_id,
            "version": version,
            "spring_boot_version": SPRING_BOOT_VERSION,
            "java_version": JAVA_VERSION,
        }


def _extract(text: str, pattern: str, default: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else default
