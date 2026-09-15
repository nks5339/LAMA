"""iter-19 — the DevOps agent's findings reach the Planner.

Before this, `_run_devops_dependency_check` ran at 97% — AFTER the run's
`final_status` and `result` had already been computed — wrote its findings
to a warn line, and returned. `production_ready: false` changed nothing.
A transformation could therefore report the green `completed` while
shipping a pom Maven cannot resolve, which is the "pom dependencies are
not getting resolved and the whole build remains incomplete" symptom.

Two things are pinned here:

  1. The audit can actually SEE the failures that break Maven. The old
     deterministic pass looked for exactly one thing — a duplicate
     `<artifactId>` — which is real but rare, and is not what generated
     poms get wrong.

  2. A finding becomes a Planner task. The repair path itself is not new:
     findings are converted into the same `error_groups` shape the
     compile-fix loop already produces, so `_planner_fix_tasks_from_errors`
     and `_coder_apply_fix` are reused unchanged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

import routes.tools as T  # noqa: E402


# ── what actually breaks Maven resolution ─────────────────────────────

POM_NO_VERSION_NO_PARENT = """<project>
  <modelVersion>4.0.0</modelVersion>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>"""

POM_UNDEFINED_PROPERTY = """<project>
  <properties><jackson.version>2.15.0</jackson.version></properties>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.5</version>
  </parent>
  <dependencies>
    <dependency>
      <groupId>org.postgresql</groupId>
      <artifactId>postgresql</artifactId>
      <version>${pg.version}</version>
    </dependency>
  </dependencies>
</project>"""

POM_WITH_PARENT = """<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.5</version>
  </parent>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>"""

POM_WITH_IMPORTED_BOM = """<project>
  <dependencyManagement><dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-dependencies</artifactId>
      <version>3.2.5</version><type>pom</type><scope>import</scope>
    </dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>"""


def test_version_less_dependency_with_nothing_to_supply_one_is_critical():
    """Maven fails with "'dependencies.dependency.version' is missing"
    before it ever reaches the network."""
    findings = T._audit_pom("backend/pom.xml", POM_NO_VERSION_NO_PARENT)
    assert any(f["severity"] == "critical" and "no <version>" in f["issue"] for f in findings)


def test_undefined_property_version_is_critical():
    """Maven does not interpolate an undeclared property; it then looks up
    the literal string `${pg.version}` as a version and fails."""
    findings = T._audit_pom("backend/pom.xml", POM_UNDEFINED_PROPERTY)
    crit = [f for f in findings if f["severity"] == "critical"]
    assert len(crit) == 1
    assert "pg.version" in crit[0]["issue"]


def test_a_declared_property_is_not_reported():
    """jackson.version IS declared in <properties> — reporting it would be
    a false positive on a perfectly valid pom."""
    findings = T._audit_pom("backend/pom.xml", POM_UNDEFINED_PROPERTY)
    assert not any("jackson" in f["issue"] for f in findings)


@pytest.mark.parametrize("pom,why", [
    (POM_WITH_PARENT, "a <parent> supplies the version"),
    (POM_WITH_IMPORTED_BOM, "an imported BOM supplies the version"),
])
def test_correct_poms_produce_no_findings(pom, why):
    """The two legitimate ways to omit a version. Flagging these would make
    the gate cry wolf on every well-formed Spring Boot pom — and since the
    gate now blocks the green badge, a false positive costs the operator a
    remediation round and an amber run."""
    assert T._audit_pom("backend/pom.xml", pom) == [], why


def test_project_builtin_properties_are_not_reported():
    """`${project.version}` is resolved by Maven itself and never appears
    in <properties>."""
    pom = POM_WITH_PARENT.replace(
        "<artifactId>spring-boot-starter-web</artifactId>",
        "<artifactId>sibling-module</artifactId><version>${project.version}</version>",
    )
    assert T._audit_pom("backend/pom.xml", pom) == []


def test_duplicate_coordinates_are_still_caught():
    """The one check the old audit had, kept — but keyed on
    groupId:artifactId rather than artifactId alone, so two different
    libraries that happen to share an artifactId are not false-flagged."""
    pom = POM_WITH_PARENT.replace(
        "</dependencies>",
        "<dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency></dependencies>",
    )
    assert any("declared more than once" in f["issue"] for f in T._audit_pom("p.xml", pom))


def test_same_artifact_id_under_different_groups_is_not_a_duplicate():
    pom = """<project><parent><version>1</version></parent><dependencies>
      <dependency><groupId>com.a</groupId><artifactId>core</artifactId></dependency>
      <dependency><groupId>com.b</groupId><artifactId>core</artifactId></dependency>
    </dependencies></project>"""
    assert not any("more than once" in f["issue"] for f in T._audit_pom("p.xml", pom))


def test_unresolvable_parent_version_is_critical():
    """A parent that cannot resolve means nothing in the module resolves."""
    pom = """<project><parent>
        <groupId>g</groupId><artifactId>a</artifactId><version>${boot.version}</version>
      </parent></project>"""
    findings = T._audit_pom("backend/pom.xml", pom)
    assert any(f["severity"] == "critical" and "parent" in f["issue"].lower() for f in findings)


def test_gradle_undefined_version_variable_is_critical():
    findings = T._audit_gradle("build.gradle", 'implementation "org.x:y:$mysteryVersion"')
    assert any(f["severity"] == "critical" for f in findings)


def test_gradle_defined_version_variable_is_clean():
    script = 'val pgVersion = "42.7.3"\nimplementation "org.postgresql:postgresql:$pgVersion"'
    assert T._audit_gradle("build.gradle", script) == []


# ── findings → Planner error groups ───────────────────────────────────

def test_findings_become_error_groups_the_planner_already_understands():
    """The repair path is reused, not reinvented: the group shape is
    exactly what `_detect_missing_dependency_error_groups` emits, so
    `_planner_fix_tasks_from_errors` consumes it unchanged."""
    findings = [
        {"severity": "critical", "manifest": "backend/pom.xml",
         "issue": "no version", "fix": "add one"},
    ]
    groups = T._devops_findings_to_error_groups(findings, {"backend": "maven"})
    assert len(groups) == 1
    g = groups[0]
    assert set(g) >= {"path", "component", "tool", "kind", "lines"}
    assert g["path"] == "backend/pom.xml"
    assert g["component"] == "backend"
    assert g["tool"] == "maven"
    # build_config is what tells the Planner to phrase the task as a
    # manifest edit rather than a source edit.
    assert g["kind"] == "build_config"
    assert "add one" in g["lines"][0][1]


def test_all_findings_for_one_manifest_become_one_task():
    """A version-less dependency and an undefined property in the same pom
    are ONE edit. Two tasks would have the second overwrite the first."""
    findings = [
        {"severity": "critical", "manifest": "backend/pom.xml", "issue": "a"},
        {"severity": "critical", "manifest": "backend/pom.xml", "issue": "b"},
        {"severity": "major", "manifest": "frontend/package.json", "issue": "c"},
    ]
    groups = T._devops_findings_to_error_groups(findings, {})
    assert len(groups) == 2
    pom = next(g for g in groups if g["path"] == "backend/pom.xml")
    assert len(pom["lines"]) == 2


def test_advisory_findings_are_not_routed_for_repair():
    """Only critical/major are worth a remediation round; spending one on
    an informational note would burn the round cap."""
    findings = [{"severity": "minor", "manifest": "backend/pom.xml", "issue": "cosmetic"}]
    assert T._devops_findings_to_error_groups(findings, {}) == []


def test_llm_severity_case_does_not_decide_the_gate():
    """The prompt asks for "CRITICAL"; the deterministic pass emits
    "critical". The routing filter must accept both — before iter-19 the
    gate compared lowercase only, so an LLM-reported CRITICAL was silently
    ignored. Harmless while the verdict was advisory; not harmless now that
    it sets the run's final status."""
    findings = [
        {"severity": "CRITICAL", "manifest": "backend/pom.xml", "issue": "shouty"},
        {"severity": "Major", "manifest": "frontend/package.json", "issue": "titlecase"},
    ]
    groups = T._devops_findings_to_error_groups(findings, {})
    assert len(groups) == 2


def test_a_finding_naming_no_file_is_not_routed():
    """"No build manifest found" is a real problem but names nothing to
    edit, so there is no task to write — it must surface in the audit
    rather than produce an unroutable group."""
    findings = [{"severity": "major", "manifest": "", "issue": "no manifest at all"}]
    assert T._devops_findings_to_error_groups(findings, {}) == []


# ── the stagnation guard ──────────────────────────────────────────────

def test_identical_findings_produce_an_identical_signature():
    """Order must not matter — the audit does not guarantee one."""
    a = [{"manifest": "p.xml", "severity": "critical", "issue": "x"},
         {"manifest": "q.xml", "severity": "major", "issue": "y"}]
    assert T._devops_findings_signature(a) == T._devops_findings_signature(list(reversed(a)))


def test_a_changed_finding_set_changes_the_signature():
    a = [{"manifest": "p.xml", "severity": "critical", "issue": "x"}]
    b = [{"manifest": "p.xml", "severity": "critical", "issue": "z"}]
    assert T._devops_findings_signature(a) != T._devops_findings_signature(b)


def test_an_emptied_finding_set_changes_the_signature():
    """The success case still has to register as progress, not stagnation."""
    a = [{"manifest": "p.xml", "severity": "critical", "issue": "x"}]
    assert T._devops_findings_signature(a) != T._devops_findings_signature([])


# ── the loop's control flow ───────────────────────────────────────────
#
# The loop spends real LLM calls per round, so every exit condition needs
# to be pinned: a loop that cannot make progress must stop rather than
# keep paying for the same failed repair.

_BAD = [{"severity": "critical", "manifest": "backend/pom.xml", "issue": "no version"}]


class _Recorder:
    """Stands in for the audit + repair machinery so the loop's decisions
    can be observed without a database or a model."""

    def __init__(self, audits):
        self.audits = list(audits)
        self.audit_calls = 0
        self.plan_calls = 0
        self.fix_calls = 0

    async def audit(self, *a, **kw):
        self.audit_calls += 1
        return self.audits[min(self.audit_calls - 1, len(self.audits) - 1)]

    async def plan(self, transform_id, groups, iteration):
        self.plan_calls += 1
        return [{"target_path": g["path"], "task_id": f"t{self.plan_calls}"} for g in groups]

    async def fix(self, *a, **kw):
        self.fix_calls += 1
        return True


@pytest.fixture
def _loop_env(monkeypatch):
    """Neutralise everything the loop touches except its own decisions."""
    async def _noop(*a, **kw):
        return None

    class _Tx:
        async def find_one(self, *a, **kw):
            return {"_id": "tx-1", "detected_stack": {}, "target_stack": {}}

    monkeypatch.setattr(T, "transformations", _Tx())
    monkeypatch.setattr(T, "_update_progress", _noop)
    monkeypatch.setattr(T, "_emit_log", lambda *a, **kw: None)

    def _rec(recorder):
        monkeypatch.setattr(T, "_run_devops_dependency_check", recorder.audit)
        monkeypatch.setattr(T, "_planner_fix_tasks_from_errors", recorder.plan)
        monkeypatch.setattr(T, "_coder_apply_fix", recorder.fix)
        return recorder

    return _rec


@pytest.mark.asyncio
async def test_a_clean_audit_spends_no_remediation_round(_loop_env):
    """The common case must cost exactly one audit and nothing else."""
    rec = _loop_env(_Recorder([{"production_ready": True, "findings": []}]))
    out = await T._run_devops_remediation_loop("tx-1", {"compilation_ready": True}, {}, None)
    assert out["audit"]["production_ready"] is True
    assert rec.plan_calls == 0
    assert rec.fix_calls == 0
    assert out["audit"]["remediation_rounds_used"] == 0


@pytest.mark.asyncio
async def test_a_repairable_finding_goes_to_the_planner_and_clears(_loop_env):
    """The whole point of the iteration: DevOps → Planner → repair →
    re-audit, ending production-ready."""
    rec = _loop_env(_Recorder([
        {"production_ready": False, "findings": _BAD},
        {"production_ready": True, "findings": []},
    ]))
    out = await T._run_devops_remediation_loop("tx-1", {"compilation_ready": True}, {}, None)
    assert rec.plan_calls == 1, "the findings never reached the Planner"
    assert rec.fix_calls == 1, "the Planner's task was never applied"
    assert out["audit"]["production_ready"] is True
    assert out["audit"]["remediation_rounds"][0]["status"] == "remediated"


@pytest.mark.asyncio
async def test_an_unchanged_finding_set_stops_the_loop(_loop_env):
    """Stagnation, not the round counter, is the primary stop condition —
    it fires on the FIRST wasted round instead of the Nth."""
    rec = _loop_env(_Recorder([{"production_ready": False, "findings": _BAD}]))
    out = await T._run_devops_remediation_loop(
        "tx-1", {"compilation_ready": True}, {}, None, max_rounds=5,
    )
    assert rec.plan_calls == 1, "a repair should be attempted exactly once"
    assert out["audit"]["remediation_rounds"][-1]["status"] == "stagnant"
    assert out["audit"]["production_ready"] is False


@pytest.mark.asyncio
async def test_the_round_cap_bounds_a_loop_that_keeps_changing(_loop_env):
    """A finding set that changes every round would slip past the
    stagnation guard forever, so the hard cap still has to hold."""
    audits = [{"production_ready": False,
               "findings": [{"severity": "critical", "manifest": "backend/pom.xml",
                             "issue": f"distinct-{i}"}]}
              for i in range(10)]
    rec = _loop_env(_Recorder(audits))
    out = await T._run_devops_remediation_loop(
        "tx-1", {"compilation_ready": True}, {}, None, max_rounds=2,
    )
    assert rec.plan_calls == 2
    assert out["audit"]["remediation_rounds_used"] == 2


@pytest.mark.asyncio
async def test_the_real_audit_feeds_the_real_planner_shape(monkeypatch):
    """End-to-end through the UNMOCKED audit.

    Every other test here stubs `_run_devops_dependency_check`, which means
    they would all still pass if the real audit emitted findings in a shape
    the group builder cannot read. This one runs the genuine deterministic
    pass over a genuinely broken pom and asserts the output flows into a
    Planner task without adaptation.
    """
    class _Cursor:
        def __aiter__(self):
            async def gen():
                yield {"path": "backend/pom.xml", "content": POM_NO_VERSION_NO_PARENT}
            return gen()

    class _Files:
        def find(self, *a, **kw):
            return _Cursor()

    async def _noop(*a, **kw):
        return None

    async def _run_id(*a, **kw):
        return "run-1"

    # Take the LLM out of the loop; the deterministic pass is what we are
    # asserting on, and it must stand on its own when the model is absent.
    async def _no_llm(*a, **kw):
        raise RuntimeError("no LLM in this test")

    monkeypatch.setattr(T, "transform_files", _Files())
    monkeypatch.setattr(T, "_log_agent_run", _run_id)
    monkeypatch.setattr(T, "_update_agent_run", _noop)
    monkeypatch.setattr(T, "_get_effective_prompt", _noop)
    monkeypatch.setattr(T, "_get_effective_model", _noop)
    monkeypatch.setattr(T, "_project_id_for_transform", _noop)
    monkeypatch.setattr(T, "fabric_call", _no_llm)
    monkeypatch.setattr(T, "transformations", type("_T", (), {
        "update_one": staticmethod(_noop)})())

    audit = await T._run_devops_dependency_check(
        "tx-1", {"compilation_ready": True}, None,
    )

    assert audit["production_ready"] is False, "a version-less dependency must block"

    # `findings` must be the UNION of deterministic + model-reported. It was
    # briefly the model's alone, which meant that with no LLM available the
    # audit stayed SILENT about a fault it had already proven by parsing —
    # the remediation loop saw nothing actionable and the UI panel rendered
    # an empty list. Both consumers read this one key.
    assert audit["deterministic"], "the deterministic pass found nothing"
    assert audit["findings"], "findings must include the deterministic pass"
    assert len(audit["findings"]) >= len(audit["deterministic"])
    assert audit["findings"][0]["severity"] == "critical", "decidable findings come first"

    groups = T._devops_findings_to_error_groups(audit["findings"], {"backend": "maven"})
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"
    assert groups[0]["kind"] == "build_config"
    # The Planner reads `lines` as [(lineno, text)] pairs.
    assert all(isinstance(ln, tuple) and len(ln) == 2 for ln in groups[0]["lines"])


@pytest.mark.asyncio
async def test_unroutable_findings_stop_rather_than_spin(_loop_env):
    """Findings that name no file cannot become tasks; the loop must say so
    instead of calling the Planner with an empty group list."""
    rec = _loop_env(_Recorder([
        {"production_ready": False,
         "findings": [{"severity": "major", "manifest": "", "issue": "no manifest"}]},
    ]))
    out = await T._run_devops_remediation_loop("tx-1", {"compilation_ready": True}, {}, None)
    assert rec.plan_calls == 0
    assert out["audit"]["remediation_rounds"][-1]["status"] == "no_actionable_findings"
