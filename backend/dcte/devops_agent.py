"""DCTE DevOps Agent — iter-18.17.

Handles *structural* migration gaps that ``build_agent.build_and_fix``
cannot: things that don't produce a ``javac`` error but leave the
migrated application non-runnable.

Common gaps this agent addresses:
  1. **Missing / incomplete ``application.yml`` (or ``application.properties``)**
     — no ``spring.datasource.*`` when the source had ``@ConfigProperty``
     for a DB URL; no ``server.port``; no ``management.endpoints.*``
     (needed by the Tester agent's ``/actuator/health`` probe).
  2. **Missing ``@SpringBootApplication`` main class** — Helidon sources
     used ``Server.builder()...start()``; the migration must produce a
     ``SpringApplication.run(...)`` entrypoint.
  3. **Missing ``spring-boot-maven-plugin``** in the ``pom.xml`` (needed
     for ``mvn spring-boot:run`` used by the Tester's boot smoke).
  4. **Missing Dockerfile** when ``generate_cicd`` contains ``docker``
     (soft-warning, not fatal).

Design
------
* Runs AFTER ``build_agent.build_and_fix`` — its job is to close
  *runtime* gaps that a green compile doesn't guarantee.
* Deterministic first, model second (iter-22, resolving HUMAN_INTERVENTION
  DT-1). Every gap with a canonical shape is patched by template, with no
  LLM call — a template is reproducible, free, and cannot invent a database
  URL. Gaps the templates cannot close used to land in ``unresolved``, get
  printed in the report, and stop there: ``agent_key="dcte.devops"`` was
  registered, tiered and seeded for an escalation that had never been
  written, so this phase resolved nothing it did not already have a
  template for. Those gaps now go to the fabric under that key
  (``_escalate_gaps_to_llm``), bounded by ``_MAX_LLM_GAP_FIXES``, with the
  reply written back through ``ai_refactor._safe_apply`` and confined to
  the service tree. ``escalate=False`` restores the old behaviour.
* Non-blocking: any gap it can't patch is emitted as a diagnostic
  event and passed to the Tester agent's report; the job continues.
* Runs INSIDE the LAMA container — no external tools required.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("lama.dcte.devops_agent")


# ── Config ──────────────────────────────────────────────────────────
# _DEFAULT_MAX_LLM_CALLS / _MAX_CHARS_PER_FILE were declared here for the
# LLM path described above and never read by anything, so they are gone
# rather than landing as dead weight. They belong with that call site
# whenever it is written.

_SPRING_APP_MAIN_TEMPLATE = """package {package};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class {cls} {{
    public static void main(String[] args) {{
        SpringApplication.run({cls}.class, args);
    }}
}}
"""

# Minimum sections a Spring Boot service NEEDS for the Tester to boot-
# smoke it: server port + actuator health endpoint exposed.
_APPLICATION_YML_MINIMUM = """server:
  port: 8080

management:
  endpoints:
    web:
      exposure:
        include: health,info
  endpoint:
    health:
      show-details: when-authorized

spring:
  application:
    name: {app_name}
"""

# Datasource block appended when the source had DB config
# (@ConfigProperty(name="db.url") etc.). Placeholders left explicit so
# the operator can slot in real credentials before deploy.
_DATASOURCE_YML_BLOCK = """  datasource:
    url: jdbc:postgresql://localhost:5432/{db_name}
    username: ${{DB_USERNAME:app}}
    password: ${{DB_PASSWORD:app}}
    driver-class-name: org.postgresql.Driver
  jpa:
    hibernate:
      ddl-auto: validate
    properties:
      hibernate:
        dialect: org.hibernate.dialect.PostgreSQLDialect
"""

_SPRING_BOOT_PLUGIN_XML = """            <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
            </plugin>
"""


@dataclass
class DevopsResult:
    """What the DevOps agent did."""
    attempted: bool = False
    fixes_applied: int = 0
    gaps_found: list[dict[str, Any]] = field(default_factory=list)
    fixes: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "fixes_applied": self.fixes_applied,
            "gaps_found": self.gaps_found,
            "fixes": self.fixes,
            "unresolved": self.unresolved,
            "notes": self.notes,
        }


# ── Scanners ────────────────────────────────────────────────────────
def _find_java_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.java") if p.is_file()]


def _has_spring_boot_main(root: Path) -> bool:
    for p in _find_java_files(root):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "@SpringBootApplication" in body and "SpringApplication.run" in body:
            return True
    return False


def _guess_root_package(root: Path) -> str:
    for p in _find_java_files(root):
        try:
            first = p.read_text(encoding="utf-8", errors="ignore")[:400]
        except Exception:
            continue
        m = re.search(r"^\s*package\s+([\w.]+)\s*;", first, re.MULTILINE)
        if m:
            return m.group(1)
    return "com.example.app"


def _find_application_config(root: Path) -> Path | None:
    for name in ("application.yml", "application.yaml", "application.properties"):
        for p in root.rglob(name):
            if p.is_file():
                return p
    return None


def _find_pom(root: Path) -> Path | None:
    for p in root.rglob("pom.xml"):
        if p.is_file():
            return p
    return None


def _source_needs_datasource(source_root: Path | None) -> str | None:
    """Return a DB name guess if source used a datasource, else None.

    Cheap heuristic: look for Helidon-era ``@ConfigProperty(name="...")``
    entries that reference ``db``, ``jdbc``, ``datasource``, or
    ``connection``. Good enough to decide whether to inject the
    datasource block into ``application.yml``.
    """
    if not source_root or not source_root.exists():
        return None
    for p in source_root.rglob("*.java"):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"@ConfigProperty\s*\(\s*name\s*=\s*\"[^\"]*(db|jdbc|datasource|connection)", body, re.I):
            return "app"
    # Also check microprofile-config.properties
    for p in source_root.rglob("microprofile-config.properties"):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"(db|jdbc|datasource)\.", body, re.I):
            return "app"
    return None


# ── Gap detection ───────────────────────────────────────────────────
def detect_structural_gaps(
    dest_root: Path,
    *,
    source_root: Path | None = None,
    build_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of gap descriptors::

        [{"kind": "missing_main_class", "severity": "high", ...}]

    Deterministic — no LLM calls.
    """
    gaps: list[dict[str, Any]] = []
    if not dest_root.exists():
        return gaps

    # 1. main class
    if not _has_spring_boot_main(dest_root):
        gaps.append({
            "kind": "missing_main_class",
            "severity": "high",
            "detail": "No @SpringBootApplication + SpringApplication.run(...) found.",
            "package_hint": _guess_root_package(dest_root),
        })

    # 2. application config
    cfg = _find_application_config(dest_root)
    if cfg is None:
        gaps.append({
            "kind": "missing_application_config",
            "severity": "high",
            "detail": "No application.yml/properties — server port and actuator health cannot be configured.",
        })
    else:
        try:
            body = cfg.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            body = ""
        missing_sections: list[str] = []
        if "server:" not in body and "server.port" not in body:
            missing_sections.append("server.port")
        if "management" not in body:
            missing_sections.append("management.endpoints (actuator)")
        needs_ds = _source_needs_datasource(source_root)
        if needs_ds and "datasource" not in body:
            missing_sections.append("spring.datasource")
        if missing_sections:
            gaps.append({
                "kind": "incomplete_application_config",
                "severity": "medium",
                "detail": f"application config missing sections: {', '.join(missing_sections)}",
                "config_path": str(cfg),
                "missing_sections": missing_sections,
                "needs_datasource": bool(needs_ds),
            })

    # 3. pom.xml spring-boot-maven-plugin
    pom = _find_pom(dest_root)
    if pom is not None:
        try:
            body = pom.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            body = ""
        if "spring-boot-maven-plugin" not in body:
            gaps.append({
                "kind": "missing_pom_plugin",
                "severity": "medium",
                "detail": "pom.xml lacks spring-boot-maven-plugin — `mvn spring-boot:run` will fail.",
                "pom_path": str(pom),
            })

    # 4. surface build errors the compile-fix loop gave up on
    if build_result and build_result.get("success") is False and not build_result.get("skipped"):
        gaps.append({
            "kind": "build_still_failing",
            "severity": "high",
            "detail": (
                f"Compile-and-fix loop exhausted after {build_result.get('attempts')} "
                f"attempt(s); {len(build_result.get('errors') or [])} error(s) remain."
            ),
            "build_result": {k: build_result.get(k) for k in
                             ("attempts", "fixes_applied", "tool", "final_output_tail")
                             if k in build_result},
        })

    return gaps


# ── Fixers ─────────────────────────────────────────────────────────
def _patch_missing_main_class(dest_root: Path, gap: dict[str, Any]) -> dict[str, Any]:
    pkg = gap.get("package_hint") or _guess_root_package(dest_root)
    cls = "Application"
    src_root = None
    for p in dest_root.rglob("src/main/java"):
        if p.is_dir():
            src_root = p
            break
    if src_root is None:
        # Fall back: put next to the first .java we find
        first = next(iter(_find_java_files(dest_root)), None)
        if first is not None:
            src_root = first.parent
    if src_root is None:
        return {"kind": gap["kind"], "resolved": False, "reason": "no src/main/java directory"}
    pkg_dir = src_root
    for part in pkg.split("."):
        pkg_dir = pkg_dir / part
    pkg_dir.mkdir(parents=True, exist_ok=True)
    target = pkg_dir / f"{cls}.java"
    if target.exists():
        return {"kind": gap["kind"], "resolved": True, "reason": "already present after re-scan"}
    body = _SPRING_APP_MAIN_TEMPLATE.format(package=pkg, cls=cls)
    target.write_text(body, encoding="utf-8")
    return {
        "kind": gap["kind"], "resolved": True,
        "file": str(target),
        "reason": f"created {target.name} with package {pkg}",
    }


def _patch_missing_application_config(
    dest_root: Path, gap: dict[str, Any], *, needs_datasource: bool
) -> dict[str, Any]:
    src_resources = None
    for p in dest_root.rglob("src/main/resources"):
        if p.is_dir():
            src_resources = p
            break
    if src_resources is None:
        # Try to create it next to src/main/java
        for p in dest_root.rglob("src/main/java"):
            src_resources = p.parent / "resources"
            src_resources.mkdir(parents=True, exist_ok=True)
            break
    if src_resources is None:
        return {"kind": gap["kind"], "resolved": False, "reason": "no src/main/resources directory"}
    app_name = dest_root.name or "app"
    target = src_resources / "application.yml"
    body = _APPLICATION_YML_MINIMUM.format(app_name=app_name)
    if needs_datasource:
        body += _DATASOURCE_YML_BLOCK.format(db_name=app_name)
    target.write_text(body, encoding="utf-8")
    return {
        "kind": gap["kind"], "resolved": True,
        "file": str(target),
        "reason": "created application.yml with server + actuator (+ datasource)"
                  if needs_datasource else
                  "created application.yml with server + actuator",
    }


def _patch_incomplete_application_config(dest_root: Path, gap: dict[str, Any]) -> dict[str, Any]:
    cfg = Path(gap.get("config_path") or "")
    if not cfg.exists():
        return {"kind": gap["kind"], "resolved": False, "reason": "config path missing"}
    if cfg.suffix.lower() in {".yml", ".yaml"}:
        try:
            body = cfg.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return {"kind": gap["kind"], "resolved": False, "reason": str(e)}
        additions: list[str] = []
        missing = set(gap.get("missing_sections") or [])
        if "server.port" in missing and "server:" not in body:
            additions.append("server:\n  port: 8080\n")
        if "management.endpoints (actuator)" in missing and "management:" not in body:
            additions.append(
                "management:\n  endpoints:\n    web:\n      exposure:\n"
                "        include: health,info\n  endpoint:\n    health:\n"
                "      show-details: when-authorized\n"
            )
        if "spring.datasource" in missing and "datasource:" not in body:
            app_name = dest_root.name or "app"
            block = "spring:\n" + _DATASOURCE_YML_BLOCK.format(db_name=app_name)
            additions.append(block)
        if not additions:
            return {"kind": gap["kind"], "resolved": True, "reason": "already contains sections after re-check"}
        new_body = body.rstrip() + "\n\n" + "\n".join(additions)
        cfg.write_text(new_body, encoding="utf-8")
        return {"kind": gap["kind"], "resolved": True, "file": str(cfg),
                "reason": f"appended: {', '.join(gap.get('missing_sections') or [])}"}
    # .properties — flat keys
    try:
        body = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"kind": gap["kind"], "resolved": False, "reason": str(e)}
    additions: list[str] = []
    missing = set(gap.get("missing_sections") or [])
    if "server.port" in missing:
        additions.append("server.port=8080")
    if "management.endpoints (actuator)" in missing:
        additions.append("management.endpoints.web.exposure.include=health,info")
        additions.append("management.endpoint.health.show-details=when-authorized")
    if not additions:
        return {"kind": gap["kind"], "resolved": True, "reason": "already contains keys after re-check"}
    cfg.write_text(body.rstrip() + "\n" + "\n".join(additions) + "\n", encoding="utf-8")
    return {"kind": gap["kind"], "resolved": True, "file": str(cfg),
            "reason": f"appended: {', '.join(gap.get('missing_sections') or [])}"}


def _patch_missing_pom_plugin(_dest_root: Path, gap: dict[str, Any]) -> dict[str, Any]:
    pom = Path(gap.get("pom_path") or "")
    if not pom.exists():
        return {"kind": gap["kind"], "resolved": False, "reason": "pom missing"}
    try:
        body = pom.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"kind": gap["kind"], "resolved": False, "reason": str(e)}
    if "spring-boot-maven-plugin" in body:
        return {"kind": gap["kind"], "resolved": True, "reason": "plugin already present"}
    # Insert into first <plugins> under <build>. If absent, append a new
    # <build><plugins>...</plugins></build> before </project>.
    if "<plugins>" in body:
        new_body = body.replace("<plugins>", "<plugins>\n" + _SPRING_BOOT_PLUGIN_XML, 1)
    elif "<build>" in body:
        new_body = body.replace(
            "<build>",
            "<build>\n        <plugins>\n" + _SPRING_BOOT_PLUGIN_XML + "        </plugins>",
            1,
        )
    elif "</project>" in body:
        new_body = body.replace(
            "</project>",
            "    <build>\n        <plugins>\n" + _SPRING_BOOT_PLUGIN_XML
            + "        </plugins>\n    </build>\n</project>",
            1,
        )
    else:
        return {"kind": gap["kind"], "resolved": False, "reason": "unrecognised pom shape"}
    pom.write_text(new_body, encoding="utf-8")
    return {"kind": gap["kind"], "resolved": True, "file": str(pom),
            "reason": "injected spring-boot-maven-plugin"}


def apply_deterministic_fixes(
    dest_root: Path, gaps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Try to close each gap deterministically. Returns
    ``(fixes, unresolved)``.
    """
    fixes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for gap in gaps:
        kind = gap.get("kind")
        try:
            if kind == "missing_main_class":
                fix = _patch_missing_main_class(dest_root, gap)
            elif kind == "missing_application_config":
                fix = _patch_missing_application_config(
                    dest_root, gap, needs_datasource=bool(gap.get("needs_datasource")),
                )
            elif kind == "incomplete_application_config":
                fix = _patch_incomplete_application_config(dest_root, gap)
            elif kind == "missing_pom_plugin":
                fix = _patch_missing_pom_plugin(dest_root, gap)
            else:
                # No deterministic fix — leave for the LLM path or report.
                unresolved.append(gap)
                continue
            if fix.get("resolved"):
                fixes.append(fix)
            else:
                unresolved.append({**gap, "reason": fix.get("reason")})
        except Exception as e:
            logger.exception("DevOps fix errored for gap %s", kind)
            unresolved.append({**gap, "reason": f"exception: {e}"})
    return fixes, unresolved


# ── LLM escalation (iter-22) ──────────────────────────────────────
#
# This is the call site DT-1 asked about. `dcte.devops` has been registered
# in AGENT_COMPLEXITY and seeded as an `agent_configs` row since iter-18,
# and the docstring above described an LLM path that had never been written:
# every gap without a template landed in `unresolved`, was printed in the
# report, and that was the end of it. The phase ran, emitted events and
# resolved nothing it did not already have a template for.
#
# Scope is deliberately narrow. The deterministic patchers keep first
# refusal — a template is reproducible and free, and a model asked to invent
# an `application.yml` will invent one. The model only sees what the
# templates could not close.
_MAX_LLM_GAP_FIXES = 4          # per run; these are whole-file writes
_MAX_LLM_FILE_CHARS = 20000


async def _escalate_gaps_to_llm(
    dest_root: Path,
    unresolved: list[dict[str, Any]],
    *,
    source_stack: str = "",
    target_stack: str = "",
    agent_key: str = "dcte.devops",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ask the model to close the gaps no template could. (fixed, still_open)."""
    if not unresolved:
        return [], []
    try:
        from llm import fabric_call
    except Exception as e:  # noqa: BLE001
        logger.warning("devops escalation unavailable: %s", e)
        return [], [{**g, "reason": f"{g.get('reason') or ''} (no LLM: {e})".strip()}
                    for g in unresolved]

    from .prompt_builder import build_devops_brief
    from .ai_refactor import _strip_json_fence, _safe_apply
    from .stacks import residue_markers

    from .prompt_builder import stack_sections
    from .prompt_store import compose, get_dcte_prompt
    system_prompt = compose(
        await get_dcte_prompt("dcte.devops"),
        stack_sections(source_stack, target_stack),
        build_devops_brief(source_stack, target_stack),
    )
    markers = residue_markers(source_stack, target_stack) or None
    fixed: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []

    for gap in unresolved[:_MAX_LLM_GAP_FIXES]:
        target = gap.get("file") or gap.get("path") or ""
        path = Path(target) if target else None
        current = ""
        if path and path.is_file():
            try:
                current = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_LLM_FILE_CHARS]
            except Exception:  # noqa: BLE001
                current = ""
        user = (
            f"---GAP: {gap.get('kind')}---\n"
            f"{gap.get('detail') or gap.get('reason') or ''}\n\n"
            f"---SERVICE ROOT: {dest_root}---\n"
            f"---TREE (up to 60 entries)---\n"
            + "\n".join(sorted(
                str(p.relative_to(dest_root))
                for p in list(dest_root.rglob("*"))[:400] if p.is_file()
            )[:60])
            + (f"\n\n---CURRENT CONTENT OF {target}---\n```\n{current}\n```"
               if current else "")
        )
        try:
            resp = await fabric_call(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user}],
                agent_key=agent_key,
                temperature=0.1,
                max_tokens=4000,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001
            still_open.append({**gap, "reason": f"escalation failed: {e}"})
            continue
        text = (resp or {}).get("content") if isinstance(resp, dict) else str(resp or "")
        try:
            data = json.loads(_strip_json_fence(text or ""))
        except Exception:  # noqa: BLE001
            still_open.append({**gap, "reason": "escalation returned non-JSON"})
            continue
        if not isinstance(data, dict) or str(data.get("action") or "") != "write":
            still_open.append({
                **gap,
                "reason": str((data or {}).get("why") or "model declined to patch"),
            })
            continue
        out_rel = str(data.get("file") or "").strip()
        content = data.get("content") or ""
        if not out_rel or not content:
            still_open.append({**gap, "reason": "escalation returned no file"})
            continue
        out_path = (dest_root / out_rel).resolve()
        # The model names the path, so it must be constrained to the service
        # tree. Without this a `../../etc/x` reply writes outside the job.
        try:
            out_path.relative_to(dest_root.resolve())
        except ValueError:
            still_open.append({**gap, "reason": f"escalation named a path outside the service: {out_rel}"})
            continue
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            still_open.append({**gap, "reason": f"mkdir failed: {e}"})
            continue
        existing = ""
        if out_path.is_file():
            try:
                existing = out_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                existing = ""
        if existing:
            ok, reason = _safe_apply(out_path, existing, content, markers)
        else:
            # A new file has no original to size-check against, so the
            # guardrail cannot apply; still refuse source-stack residue.
            bad = next((m for m in (markers or ()) if m in content), "")
            if bad:
                ok, reason = False, f"rejected: new file carries legacy marker '{bad}'"
            else:
                try:
                    out_path.write_text(content, encoding="utf-8")
                    ok, reason = True, "created"
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"write failed: {e}"
        if ok:
            fixed.append({
                "kind": gap.get("kind"), "resolved": True, "by": "llm",
                "file": str(out_path),
                "reason": str(data.get("why") or "patched by the DevOps agent"),
            })
        else:
            still_open.append({**gap, "reason": f"escalation rejected: {reason}"})

    # Anything past the per-run cap was never looked at — say so rather than
    # letting it read as "the model could not fix it".
    for gap in unresolved[_MAX_LLM_GAP_FIXES:]:
        still_open.append({**gap, "reason": "not escalated (per-run cap reached)"})
    return fixed, still_open


# ── Public entrypoint ─────────────────────────────────────────────
async def run_devops(
    dest_root: Path,
    *,
    source_root: Path | None = None,
    build_result: dict[str, Any] | None = None,
    progress_cb=None,
    source_stack: str = "",
    target_stack: str = "",
    escalate: bool = True,
) -> DevopsResult:
    """Scan the destination tree for structural migration gaps and
    apply the deterministic fixes.  Non-blocking: unresolved gaps are
    returned in the result for the Tester agent / report to surface.

    iter-22 — gaps the templates cannot close are escalated to the model
    under ``agent_key="dcte.devops"`` before being reported as unresolved.
    Pass ``escalate=False`` for a purely deterministic run.
    """
    result = DevopsResult(attempted=True)
    gaps = detect_structural_gaps(
        dest_root, source_root=source_root, build_result=build_result,
    )
    result.gaps_found = gaps
    if not gaps:
        result.notes.append("no structural gaps detected")
        return result
    if progress_cb:
        try:
            progress_cb("scan", len(gaps))
        except Exception:
            pass
    result.notes.append(f"detected {len(gaps)} structural gap(s)")
    fixes, unresolved = apply_deterministic_fixes(dest_root, gaps)

    if unresolved and escalate:
        if progress_cb:
            try:
                progress_cb("escalate", len(unresolved))
            except Exception:
                pass
        result.notes.append(
            f"escalating {len(unresolved)} gap(s) with no deterministic fix to the model"
        )
        llm_fixes, unresolved = await _escalate_gaps_to_llm(
            dest_root, unresolved,
            source_stack=source_stack, target_stack=target_stack,
        )
        fixes = fixes + llm_fixes

    result.fixes = fixes
    result.fixes_applied = len(fixes)
    result.unresolved = unresolved
    for fix in fixes:
        via = " (model)" if fix.get("by") == "llm" else ""
        result.notes.append(f"fixed{via} [{fix.get('kind')}]: {fix.get('reason')}")
    for u in unresolved:
        result.notes.append(
            f"UNRESOLVED [{u.get('kind')}]: {u.get('detail') or u.get('reason', '')}"
        )
    if progress_cb:
        try:
            progress_cb("done", len(fixes))
        except Exception:
            pass
    return result


__all__ = [
    "DevopsResult",
    "detect_structural_gaps",
    "apply_deterministic_fixes",
    "run_devops",
]
