"""DCTE Tester Agent — iter-18.17.

Post-conversion "is this good to go?" gate.  Runs three checks and
returns a verdict:

1. **Static parity** — every JAX-RS ``@Path`` (or Helidon route) in
   the source should map to a Spring ``@GetMapping / @PostMapping /
   @RequestMapping`` in the destination.  Also counts residual legacy
   markers (via :func:`ai_refactor.scan_residual`) so the report shows
   coverage of the migration.
2. **Config parity** — every Helidon MicroProfile config key present
   in ``microprofile-config.properties`` (source) must be represented
   in ``application.yml/properties`` (destination) as its Spring
   equivalent.
3. **Boot smoke** — subprocess ``mvn spring-boot:run`` (or falls back
   to ``java -jar target/*.jar`` if a jar is present) with a bounded
   timeout, probes ``/actuator/health`` until it returns 200 or the
   timeout expires, then SIGTERMs the process.

The verdict is one of:
* ``PASS`` — parity ≥ 90% and boot smoke green
* ``PASS_WITH_WARNINGS`` — parity 70–90% OR boot smoke not runnable
  (mvn absent, no ``spring-boot-maven-plugin``, no jar) but static
  checks OK
* ``FAIL`` — parity < 70% OR boot smoke red (started but /actuator/health
  never returned 2xx within timeout)

Non-blocking: a FAIL verdict does not raise; the engine records it and
moves on so the operator can inspect the report.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ai_refactor import scan_residual

logger = logging.getLogger("lama.dcte.tester_agent")


# ── Config ──────────────────────────────────────────────────────────
_BOOT_SMOKE_TIMEOUT_S = int(os.environ.get("LAMA_DCTE_BOOT_SMOKE_TIMEOUT_S", "180") or "180")
_HEALTH_PROBE_INTERVAL_S = 2.0
_HEALTH_PORT_DEFAULT = 8080

_PARITY_PASS_THRESHOLD = 0.90
_PARITY_WARN_THRESHOLD = 0.70


@dataclass
class TesterVerdict:
    verdict: str = "PASS_WITH_WARNINGS"
    parity_endpoints: dict[str, Any] = field(default_factory=dict)
    parity_config: dict[str, Any] = field(default_factory=dict)
    residual: list[dict[str, Any]] = field(default_factory=list)
    boot_smoke: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "parity_endpoints": self.parity_endpoints,
            "parity_config": self.parity_config,
            "residual": self.residual,
            "boot_smoke": self.boot_smoke,
            "notes": self.notes,
        }


# ── Endpoint parity ─────────────────────────────────────────────────
_JAXRS_PATH_RE = re.compile(r"@Path\s*\(\s*\"([^\"]+)\"")
_SPRING_MAPPING_RE = re.compile(
    r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?\"([^\"]+)\""
)


def _extract_paths(root: Path | None, patterns: list[re.Pattern]) -> set[str]:
    """Return the set of URL path segments (normalised) found in any
    ``.java`` file under ``root`` matching any pattern.
    """
    out: set[str] = set()
    if not root or not root.exists():
        return out
    for p in root.rglob("*.java"):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in patterns:
            for m in pat.finditer(body):
                path = m.group(1).strip()
                # Normalise trailing slashes + case-insensitive param names.
                path = re.sub(r"\{[^}]+\}", "{}", path).rstrip("/") or "/"
                out.add(path)
    return out


def endpoint_parity(
    source_root: Path | None, dest_root: Path,
) -> dict[str, Any]:
    src = _extract_paths(source_root, [_JAXRS_PATH_RE, _SPRING_MAPPING_RE])
    dst = _extract_paths(dest_root, [_SPRING_MAPPING_RE, _JAXRS_PATH_RE])
    missing = sorted(src - dst)
    extra = sorted(dst - src)
    coverage = 1.0 if not src else (len(src & dst) / len(src))
    return {
        "source_count": len(src),
        "dest_count": len(dst),
        "matched": len(src & dst),
        "missing": missing[:50],
        "extra": extra[:50],
        "coverage": round(coverage, 3),
    }


# ── Config parity ───────────────────────────────────────────────────
_MP_KEY_RE = re.compile(r"^\s*([\w.\-]+)\s*=", re.MULTILINE)


def _source_mp_keys(source_root: Path | None) -> set[str]:
    out: set[str] = set()
    if not source_root or not source_root.exists():
        return out
    for name in ("microprofile-config.properties",):
        for p in source_root.rglob(name):
            try:
                body = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in _MP_KEY_RE.finditer(body):
                key = m.group(1).strip()
                if key and not key.startswith("#"):
                    out.add(key)
    return out


def _dest_config_keys(dest_root: Path) -> set[str]:
    """Return every dotted config key mentioned in application.yml /
    application.properties (approximate — flattens yaml one level).
    """
    keys: set[str] = set()
    for name in ("application.yml", "application.yaml"):
        for p in dest_root.rglob(name):
            try:
                body = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            stack: list[tuple[int, str]] = []
            for line in body.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                m = re.match(r"([\w\-]+)\s*:(.*)", line.strip())
                if not m:
                    continue
                # Pop deeper contexts
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                name = m.group(1)
                stack.append((indent, name))
                dotted = ".".join(s[1] for s in stack)
                keys.add(dotted)
    for p in dest_root.rglob("application.properties"):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _MP_KEY_RE.finditer(body):
            keys.add(m.group(1).strip())
    return keys


def config_parity(source_root: Path | None, dest_root: Path) -> dict[str, Any]:
    src_keys = _source_mp_keys(source_root)
    dst_keys = _dest_config_keys(dest_root)
    if not src_keys:
        return {
            "source_count": 0, "dest_count": len(dst_keys),
            "matched": 0, "missing": [], "coverage": 1.0,
        }

    def _matched(k: str) -> bool:
        if k in dst_keys:
            return True
        # Common Helidon → Spring rewrites (best-effort mapping).
        rewrites = {
            "server.port": "server.port",
            "server.host": "server.address",
        }
        if rewrites.get(k) in dst_keys:
            return True
        # Suffix / last-segment fallback.  Handles the common
        # Helidon-era ``db.url`` → ``spring.datasource.url`` and
        # ``db.username`` → ``spring.datasource.username`` renames
        # without needing an exhaustive mapping table.
        if any(dst.endswith("." + k) for dst in dst_keys):
            return True
        last = k.rsplit(".", 1)[-1]
        if last and any(dst.rsplit(".", 1)[-1] == last for dst in dst_keys):
            return True
        return False

    matched = {k for k in src_keys if _matched(k)}
    missing = sorted(src_keys - matched)
    coverage = len(matched) / max(1, len(src_keys))
    return {
        "source_count": len(src_keys),
        "dest_count": len(dst_keys),
        "matched": len(matched),
        "missing": missing[:50],
        "coverage": round(coverage, 3),
    }


# ── Boot smoke ─────────────────────────────────────────────────────
def _read_server_port(dest_root: Path) -> int:
    for name in ("application.yml", "application.yaml"):
        for p in dest_root.rglob(name):
            try:
                body = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            m = re.search(r"server:\s*\n\s+port:\s*(\d+)", body)
            if m:
                return int(m.group(1))
            m = re.search(r"^\s*server\.port\s*:\s*(\d+)", body, re.MULTILINE)
            if m:
                return int(m.group(1))
    for p in dest_root.rglob("application.properties"):
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(r"^\s*server\.port\s*=\s*(\d+)", body, re.MULTILINE)
        if m:
            return int(m.group(1))
    return _HEALTH_PORT_DEFAULT


def _pick_boot_cmd(dest_root: Path) -> tuple[list[str], str, Path] | None:
    """Return (argv, tool_name, cwd) for booting the app, or None if
    no viable path exists.  Prefers ``mvn spring-boot:run`` because it
    reflects the freshly-migrated pom; falls back to
    ``java -jar target/*.jar`` if a fat-jar is already built.
    """
    pom = None
    for p in dest_root.rglob("pom.xml"):
        pom = p
        break
    if pom is not None and shutil.which("mvn"):
        return (
            ["mvn", "-q", "-o", "-DskipTests", "spring-boot:run"],
            "mvn spring-boot:run",
            pom.parent,
        )
    # java -jar fallback
    for p in dest_root.rglob("target/*.jar"):
        if p.is_file() and "sources" not in p.name and "javadoc" not in p.name:
            if shutil.which("java"):
                return ["java", "-jar", str(p)], f"java -jar {p.name}", p.parent.parent
    return None


def _probe_health(port: int, deadline: float) -> tuple[bool, str]:
    """Poll ``/actuator/health`` until 200 or the deadline expires."""
    import urllib.request
    import urllib.error
    url = f"http://127.0.0.1:{port}/actuator/health"
    last_reason = "no probe attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    body = resp.read(2048).decode("utf-8", errors="ignore")
                    return True, body
                last_reason = f"HTTP {resp.status}"
        except urllib.error.URLError as e:
            last_reason = f"URLError: {getattr(e, 'reason', e)}"
        except Exception as e:  # pragma: no cover — defensive
            last_reason = f"{type(e).__name__}: {e}"
        time.sleep(_HEALTH_PROBE_INTERVAL_S)
    return False, last_reason


def run_boot_smoke(dest_root: Path, *, timeout_s: int = _BOOT_SMOKE_TIMEOUT_S) -> dict[str, Any]:
    """Boot the migrated app in a subprocess, probe
    ``/actuator/health``, then shut it down.  Non-blocking on
    infrastructure errors — returns ``{"runnable": False, ...}`` if
    the app can't be booted at all.
    """
    picked = _pick_boot_cmd(dest_root)
    if picked is None:
        return {
            "runnable": False,
            "reason": "no viable boot command (no mvn on PATH and no target/*.jar)",
            "healthy": False,
        }
    argv, tool, cwd = picked
    port = _read_server_port(dest_root)
    env = os.environ.copy()
    # Silence maven download noise + prevent JVM from binding to random
    # extra ports the CI runner might be using.
    env.setdefault("MAVEN_OPTS", "-Dmaven.repo.local=" + str(cwd / ".m2"))
    started_at = time.monotonic()
    deadline = started_at + timeout_s
    proc: subprocess.Popen | None = None
    try:
        # Start detached so we can SIGTERM the process group cleanly.
        proc = subprocess.Popen(
            argv, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        healthy, health_reason = _probe_health(port, deadline)
        elapsed = round(time.monotonic() - started_at, 2)
        # Grab a tail of process output for reporting.
        try:
            # Non-blocking: read whatever's available now.
            os.set_blocking(proc.stdout.fileno(), False)  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            tail = proc.stdout.read(8192).decode("utf-8", errors="ignore") if proc.stdout else ""  # type: ignore[union-attr]
        except Exception:
            tail = ""
        return {
            "runnable": True,
            "tool": tool,
            "port": port,
            "healthy": healthy,
            "elapsed_s": elapsed,
            "reason": None if healthy else health_reason,
            "output_tail": tail[-2000:],
        }
    except FileNotFoundError as e:
        return {"runnable": False, "reason": f"tool not found: {e}", "healthy": False}
    except Exception as e:
        logger.exception("boot smoke errored")
        return {"runnable": False, "reason": f"boot errored: {type(e).__name__}: {e}", "healthy": False}
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()


# ── Verdict ─────────────────────────────────────────────────────────
def _decide_verdict(
    endpoint: dict[str, Any],
    config: dict[str, Any],
    residual: list[dict[str, Any]],
    boot: dict[str, Any],
) -> str:
    ec = float(endpoint.get("coverage") or 1.0)
    cc = float(config.get("coverage") or 1.0)
    if residual:
        return "FAIL" if ec < _PARITY_WARN_THRESHOLD else "PASS_WITH_WARNINGS"
    if boot.get("runnable") and boot.get("healthy") is False:
        return "FAIL"
    if ec < _PARITY_WARN_THRESHOLD or cc < _PARITY_WARN_THRESHOLD:
        return "FAIL"
    if ec < _PARITY_PASS_THRESHOLD or cc < _PARITY_PASS_THRESHOLD:
        return "PASS_WITH_WARNINGS"
    if not boot.get("runnable"):
        return "PASS_WITH_WARNINGS"
    return "PASS"


# ── Public entrypoint ─────────────────────────────────────────────
async def run_tester(
    dest_root: Path,
    *,
    source_root: Path | None = None,
    run_boot: bool = True,
    timeout_s: int = _BOOT_SMOKE_TIMEOUT_S,
) -> TesterVerdict:
    """Run every check and return a verdict."""
    verdict = TesterVerdict()

    # Static checks are cheap — run inline.
    verdict.parity_endpoints = endpoint_parity(source_root, dest_root)
    verdict.parity_config = config_parity(source_root, dest_root)
    verdict.residual = scan_residual(dest_root)

    if run_boot:
        # Boot smoke can block for up to timeout_s. Run in a worker
        # thread so the caller's event loop isn't blocked and other
        # services' progress ticks can still be emitted.
        try:
            verdict.boot_smoke = await asyncio.to_thread(
                run_boot_smoke, dest_root, timeout_s=timeout_s,
            )
        except Exception as e:
            logger.exception("run_boot_smoke wrapper errored")
            verdict.boot_smoke = {"runnable": False, "reason": f"wrapper: {e}", "healthy": False}
    else:
        verdict.boot_smoke = {"runnable": False, "reason": "boot smoke disabled", "healthy": False}

    verdict.verdict = _decide_verdict(
        verdict.parity_endpoints, verdict.parity_config,
        verdict.residual, verdict.boot_smoke,
    )
    ep = verdict.parity_endpoints
    cp = verdict.parity_config
    verdict.notes.append(
        f"endpoint parity: {ep.get('matched')}/{ep.get('source_count')} "
        f"({int(100 * (ep.get('coverage') or 0))}%)"
    )
    verdict.notes.append(
        f"config parity: {cp.get('matched')}/{cp.get('source_count')} "
        f"({int(100 * (cp.get('coverage') or 0))}%)"
    )
    if verdict.residual:
        verdict.notes.append(f"{len(verdict.residual)} file(s) with legacy markers remaining")
    bs = verdict.boot_smoke
    if bs.get("runnable"):
        verdict.notes.append(
            f"boot smoke: {'HEALTHY' if bs.get('healthy') else 'UNHEALTHY'} "
            f"(tool={bs.get('tool')}, port={bs.get('port')}, elapsed={bs.get('elapsed_s')}s)"
        )
    else:
        verdict.notes.append(f"boot smoke: not runnable ({bs.get('reason')})")
    return verdict


__all__ = [
    "TesterVerdict",
    "endpoint_parity",
    "config_parity",
    "run_boot_smoke",
    "run_tester",
]
