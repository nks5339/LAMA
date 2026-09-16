"""Cross-service report writer (traceability + impact)."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any


class ReportGenerator:
    def write_traceability(self, output_root: str, rows: list[dict[str, Any]]) -> Path:
        out = Path(output_root) / "reports" / "traceability_matrix.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Transformation Traceability Matrix",
            "",
            "| Service | Plugin | Source | Target | Kind | Status | Δ lines |",
            "|---------|--------|--------|--------|------|--------|---------|",
        ]
        for r in rows:
            lines.append(
                f"| {r.get('service','')} | {r.get('plugin','')} | "
                f"`{r.get('source','')}` | `{r.get('target','')}` | "
                f"{r.get('kind','')} | {r.get('status','')} | {r.get('lines_changed',0)} |"
            )
        out.write_text("\n".join(lines), encoding="utf-8")
        (out.with_suffix(".json")).write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )
        return out

    def write_impact(self, output_root: str, impact: dict[str, Any]) -> Path:
        out = Path(output_root) / "reports" / "impact_analysis.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Impact Analysis",
            "",
            f"- **Services**: {impact.get('services')}",
            f"- **Files transformed (success)**: {impact.get('files_transformed')}",
            f"- **Files needing manual intervention**: {impact.get('files_needs_manual')}",
            f"- **Files failed**: {impact.get('files_failed')}",
            f"- **Risk level**: {impact.get('risk_level')}",
            f"- **Complexity score (0-100)**: {impact.get('complexity_score')}",
            f"- **Estimated effort**: {impact.get('effort_hours')} hours "
            f"(~{impact.get('effort_person_days')} person-days)",
            "",
            "> Effort estimation uses a deterministic weighted model "
            "(0.25h auto / 2.5h manual / 4h failed) with a risk multiplier.",
        ]
        out.write_text("\n".join(lines), encoding="utf-8")
        (out.with_suffix(".json")).write_text(
            json.dumps(impact, indent=2), encoding="utf-8"
        )
        return out
