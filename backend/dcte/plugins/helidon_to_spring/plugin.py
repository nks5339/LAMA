"""Helidon MicroProfile → Spring Boot 3 plugin."""
from __future__ import annotations
import re
import shutil
from pathlib import Path

from ...plugin_base import (
    TransformationPlugin, TransformContext,
    AnalysisResult, TransformResult, TransformedFile,
    ValidationResult, ReportBundle,
)
from ...dependency_migrator import DependencyMigrator
from ...validator import BasicValidator
from .endpoint_transformer import transform_java_file
from .config_transformer import translate as translate_mp_config
from .bootstrap_writer import write_application_class, write_security_config, write_openapi_config


_JAXRS_MARKERS = ("jakarta.ws.rs", "@Path", "@GET", "@POST", "@PUT", "@DELETE", "@Inject")
_HELIDON_MARKERS = (
    "io.helidon",
    "org.eclipse.microprofile",
    "Routing.builder",           # Helidon SE
    "WebServer.builder",         # Helidon SE
    "ServerRequest",             # Helidon SE handler signatures
    "ServerResponse",
    "Config.create",             # Helidon Config
)


class HelidonToSpringPlugin(TransformationPlugin):
    id = "helidon-mp-to-spring-boot-3"
    display_name = "Helidon MicroProfile → Spring Boot 3"
    source_stack = "helidon-mp"
    target_stack = "spring-boot-3"
    version = "0.1.0"

    def analyze(self, ctx: TransformContext) -> AnalysisResult:
        result = AnalysisResult()
        source = ctx.module_path or ctx.source_path
        if not source.exists():
            result.risk_level = "high"
            result.findings.append({"level": "error", "message": f"Source path missing: {source}"})
            return result

        # iter-18.4 — never re-walk our own output; also skip build junk.
        _SKIP_DIRS = {"target", "build", "node_modules", ".git", ".idea", ".gradle", "out"}

        def _keep(p: Path) -> bool:
            if ctx.under_output_root(p):
                return False
            parts = set(p.parts)
            return not (parts & _SKIP_DIRS)

        java_files = [f for f in source.rglob("*.java") if _keep(f)]
        result.files_scanned = len(java_files)
        for f in java_files:
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if any(mk in txt for mk in _JAXRS_MARKERS) or any(mk in txt for mk in _HELIDON_MARKERS):
                result.matched_files.append(str(f))
            if "io.helidon.security.jwt" in txt or "OidcConfig" in txt:
                result.findings.append({
                    "level": "warn", "file": str(f),
                    "message": "Uses Helidon JWT/OIDC — Spring Security config generated as stub, wire manually.",
                })
                result.unsupported.append(str(f))

        # risk & effort
        if len(result.matched_files) > 50:
            result.risk_level = "high"
        elif len(result.matched_files) > 15:
            result.risk_level = "medium"
        else:
            result.risk_level = "low"
        result.effort_hours = len(result.matched_files) * 0.35
        result.complexity_score = min(100.0, len(result.matched_files) * 1.2 + len(result.unsupported) * 5)
        return result

    def transform(self, ctx: TransformContext, analysis: AnalysisResult) -> TransformResult:
        result = TransformResult()
        source = ctx.module_path or ctx.source_path
        dest = ctx.destination_path
        base_pkg = ctx.options.get("base_package") or self._guess_base_package(source)

        # iter-18.6 — Walk EVERY .java file (not just the regex-matched ones).
        # Reason: files that use Helidon SE (functional Routing.builder API) or
        # are plain DTOs/services carry NO JAX-RS/MP markers, so `matched_files`
        # misses them and they never make it into `dest`. That is why users
        # reported "converted files are identical to originals" — those files
        # were being copied verbatim (or dropped entirely, since step-5 skips
        # .java). Now every .java is mirrored to dest and the regex applied;
        # the LLM transformer (step 6) sees the full tree and can finish the
        # migration for anything the regex could not resolve.
        _SKIP_DIRS = {"target", "build", "node_modules", ".git", ".idea", ".gradle", "out"}

        def _java_ok(p: Path) -> bool:
            if ctx.under_output_root(p):
                return False
            return not (set(p.parts) & _SKIP_DIRS)

        matched_set = set(analysis.matched_files)
        all_java = [f for f in source.rglob("*.java") if _java_ok(f)]
        for src in all_java:
            rel = src.relative_to(source)
            tgt = dest / rel
            try:
                changed, meta = transform_java_file(src, tgt)
                was_matched = str(src) in matched_set
                if changed:
                    status = "success"
                    notes = (f"paths={meta.get('class_paths')} endpoints={len(meta.get('endpoints', []))}"
                             if meta.get("class_paths") else "regex rewrites applied")
                elif was_matched:
                    # matched by markers but the regex found nothing to change
                    # → LLM will handle it
                    status = "success"
                    notes = "regex no-op; queued for AI transformer"
                else:
                    # untouched by markers — pure passthrough
                    status = "success"
                    notes = "no Helidon markers; copied verbatim (AI may still rewrite)"
                result.files.append(TransformedFile(
                    source=str(src), target=str(tgt),
                    kind="rest_controller" if meta.get("class_paths") else "java_source",
                    status=status,
                    lines_changed=self._count_line_delta(src, tgt),
                    notes=notes,
                ))
            except Exception as e:
                result.files.append(TransformedFile(
                    source=str(src), target=str(tgt),
                    kind="java_source", status="failed",
                    notes=f"transform error: {e}",
                ))

        # 2) microprofile-config.properties → application.properties
        for mp in source.rglob("microprofile-config.properties"):
            tgt = dest / "src" / "main" / "resources" / "application.properties"
            try:
                total, rewritten = translate_mp_config(mp, tgt)
                result.files.append(TransformedFile(
                    source=str(mp), target=str(tgt),
                    kind="config", status="success",
                    lines_changed=rewritten,
                    notes=f"{rewritten}/{total} keys rewritten",
                ))
            except Exception as e:
                result.files.append(TransformedFile(
                    source=str(mp), target=str(tgt),
                    kind="config", status="failed",
                    notes=f"config translate error: {e}",
                ))

        # 3) pom.xml — build a Spring Boot 3 / Java 21 POM.
        pom = source / "pom.xml"
        if pom.exists():
            tgt_pom = dest / "pom.xml"
            try:
                changes = DependencyMigrator().migrate_pom(pom, tgt_pom)
                result.dependency_changes = changes
                result.files.append(TransformedFile(
                    source=str(pom), target=str(tgt_pom),
                    kind="pom", status="success", lines_changed=999,
                    notes=f"Spring Boot {changes['spring_boot_version']} / Java {changes['java_version']}",
                ))
            except Exception as e:
                result.files.append(TransformedFile(
                    source=str(pom), target=str(tgt_pom),
                    kind="pom", status="failed",
                    notes=f"pom migrate error: {e}",
                ))

        # 4) Synthesise Application.java + SecurityConfig.java + OpenApiConfig.java.
        app = write_application_class(dest, base_pkg)
        sec = write_security_config(dest, base_pkg)
        oapi = write_openapi_config(dest, base_pkg, title=ctx.service_id)
        result.generated_files.extend([str(app), str(sec), str(oapi)])

        # 5) Copy static resources verbatim (non-.java, non-.properties).
        _SKIP_DIRS = {"target", "build", "node_modules", ".git", ".idea", ".gradle", "out"}
        for extra in source.rglob("*"):
            if not extra.is_file():
                continue
            if ctx.under_output_root(extra):  # iter-18.4 — skip our own output
                continue
            if set(extra.parts) & _SKIP_DIRS:
                continue
            if extra.suffix in (".java", ".properties") or extra.name == "pom.xml":
                continue
            # Skip binary artefacts + editor/CI cruft.
            if extra.suffix in (".class", ".jar", ".war", ".log"):
                continue
            rel = extra.relative_to(source)
            tgt = dest / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(extra, tgt)
            except Exception:
                pass

        # 7) Emit a .gitignore + README so the destination is a proper repo.
        try:
            (dest / ".gitignore").write_text(
                "target/\n.idea/\n*.iml\n.DS_Store\n*.class\n*.log\n",
                encoding="utf-8",
            )
            (dest / "README.md").write_text(self._readme_md(analysis), encoding="utf-8")
            result.generated_files.extend([str(dest / ".gitignore"), str(dest / "README.md")])
        except Exception:
            pass

        # 8) iter-21 — complete the manifest from what the code imports.
        #
        # Runs LAST, after every .java file is on disk, because that is the
        # only point where the full import set exists. This is the step the
        # operator asked for: "make it import the packages easily like every
        # IDE like vscode or claude does". Carrying the source pom across
        # (step 3) handles direct dependencies; this catches the two cases
        # it cannot see — something the source got transitively through a
        # Helidon bundle we dropped, and anything the AI pass introduced.
        try:
            from ...dependency_migrator import complete_pom_from_sources_sync
            dep_sweep = complete_pom_from_sources_sync(dest, own_group=base_pkg)
            result.dependency_changes = {
                **(result.dependency_changes or {}),
                "auto_added": dep_sweep.get("added", []),
                "auto_added_for": dep_sweep.get("added_for", {}),
            }
            if dep_sweep.get("added"):
                ctx.emit("info", "transform",
                         f"Resolved {len(dep_sweep['added'])} missing dependenc"
                         f"{'y' if len(dep_sweep['added']) == 1 else 'ies'} from imports: "
                         + ", ".join(dep_sweep["added"][:6]))
            # A `jakarta.*` import whose javax original is a JDK package is
            # a rename that went too far. No dependency can fix it, so it is
            # surfaced as manual intervention rather than left to the build.
            # An import still pointing at the OLD framework means the code
            # migration missed a file. Reported, never silently satisfied.
            for left in dep_sweep.get("unmigrated_imports", []):
                result.manual_intervention.append({
                    "level": "error", "kind": "unmigrated_import",
                    "detail": left,
                    "fix": f"`{left}` belongs to the source stack. A dependency "
                           f"for it was deliberately NOT added — adding one "
                           f"would make the build pass while the service still "
                           f"runs on the old framework. Migrate the file.",
                })
                ctx.emit("warn", "transform", f"unmigrated import left behind: {left}")
            for bad in dep_sweep.get("bad_jakarta", []):
                result.manual_intervention.append({
                    "level": "error", "kind": "invalid_jakarta_rename",
                    "detail": bad,
                    "fix": "This javax package is part of the JDK and did not "
                           "move to the jakarta namespace. Revert the import "
                           "to javax.*",
                })
                ctx.emit("warn", "transform", f"invalid jakarta rename: {bad}")
        except Exception as e:  # noqa: BLE001
            ctx.emit("warn", "transform", f"dependency sweep skipped: {e}")

        # 6) Manual-intervention hints from JWT/OIDC finding.
        for f in analysis.findings:
            if f.get("level") == "warn":
                result.manual_intervention.append(f)
        ctx.emit("info", "transform",
                 f"HelidonToSpring produced {len(result.files)} files, "
                 f"{len(result.generated_files)} generated")
        return result

    def validate(self, _ctx: TransformContext, result: TransformResult) -> ValidationResult:
        v = ValidationResult()
        bv = BasicValidator()
        for tf in result.files:
            if tf.status != "success" or not tf.target.endswith(".java"):
                continue
            diags = bv.check_java_balance(Path(tf.target))
            if diags:
                v.syntax_ok = False
                v.diagnostics.extend(diags)
        # If the pom was written, dependency resolution is "assumed ok".
        v.dependency_resolution_ok = any(tf.kind == "pom" and tf.status == "success" for tf in result.files) \
            or v.dependency_resolution_ok
        return v

    def generate_report(
        self, ctx: TransformContext, analysis: AnalysisResult,
        result: TransformResult, validation: ValidationResult,
    ) -> ReportBundle:
        b = ReportBundle()
        endpoints = [tf for tf in result.files if tf.kind == "rest_controller"]
        b.summary_md = (
            "# Migration Summary — Helidon MP → Spring Boot 3\n"
            f"\n- Service: **{ctx.service_id}**\n"
            f"- Files scanned: **{analysis.files_scanned}**\n"
            f"- Files transformed: **{sum(1 for f in result.files if f.status == 'success')}**\n"
            f"- REST controllers rewritten: **{len(endpoints)}**\n"
            f"- Generated files: **{len(result.generated_files)}**\n"
            f"- Manual intervention items: **{len(result.manual_intervention)}**\n"
            f"- Risk level: **{analysis.risk_level}**\n"
            f"- Estimated effort: **{analysis.effort_hours:.1f} h**\n"
        )
        b.api_change_md = "# API Change Report\n\n" + "\n".join(
            f"- `{tf.source}` → `{tf.target}` — {tf.notes or ''}"
            for tf in endpoints
        )
        b.quality_md = "# Code Quality Report\n\n" + "\n".join(
            f"- **{d.get('level','')}** — {d.get('file','')}: {d.get('message','')}"
            for d in validation.diagnostics[:200]
        ) or "# Code Quality Report\n\nNo diagnostics reported."
        b.metrics = {
            "risk_level": analysis.risk_level,
            "controllers": len(endpoints),
            "generated": len(result.generated_files),
            "manual": len(result.manual_intervention),
        }
        return b

    # ------------------------------------------------------------------
    def _guess_base_package(self, source: Path) -> str:
        for j in source.rglob("*.java"):
            try:
                for line in j.read_text(encoding="utf-8", errors="ignore").splitlines():
                    m = re.match(r"\s*package\s+([\w\.]+)\s*;", line)
                    if m:
                        return m.group(1)
            except Exception:
                continue
        return "com.example.app"

    def _count_line_delta(self, src: Path, tgt: Path) -> int:
        try:
            a = src.read_text(encoding="utf-8", errors="ignore").splitlines()
            b = tgt.read_text(encoding="utf-8", errors="ignore").splitlines()
            return abs(len(a) - len(b))
        except Exception:
            return 0

    def _readme_md(self, analysis: AnalysisResult) -> str:
        return (
            "# Migrated by DCTE — Helidon MicroProfile → Spring Boot 3\n"
            "\n"
            "This tree was produced by the Direct Code Transformation Engine\n"
            f"(plugin `{self.id}` v{self.version}).\n"
            "\n"
            "## What was rewritten\n"
            "- JAX-RS `@Path`, `@GET`/`@POST`/`@PUT`/`@DELETE` → Spring `@RestController` / `@*Mapping`.\n"
            "- CDI `@ApplicationScoped`, `@Inject` → Spring `@Component`, `@Autowired`.\n"
            "- MicroProfile `@ConfigProperty` → Spring `@Value`.\n"
            "- MP Health checks → Spring Actuator health indicators.\n"
            "- MP Metrics → Micrometer.\n"
            "- MP OpenAPI → SpringDoc OpenAPI.\n"
            "- `microprofile-config.properties` → `src/main/resources/application.properties`.\n"
            "- `pom.xml` → Spring Boot 3.3 / Java 21 with starters (web, actuator, security, data-jpa, validation).\n"
            "- Generated `Application.java` (@SpringBootApplication) + `SecurityConfig.java` (Spring Security stub).\n"
            "\n"
            "## Run\n"
            "```bash\n"
            "mvn spring-boot:run\n"
            "# or\n"
            "mvn clean package && java -jar target/*.jar\n"
            "```\n"
            "\n"
            f"## Stats\n- Files scanned: **{analysis.files_scanned}**\n"
            f"- Matched (transformed): **{len(analysis.matched_files)}**\n"
            f"- Manual-review items: **{len(analysis.unsupported)}**\n"
            f"- Risk: **{analysis.risk_level}** · Effort: **{analysis.effort_hours:.1f} h**\n"
        )
