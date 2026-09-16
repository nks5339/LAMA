"""Impact analysis — deterministic effort estimation."""
from __future__ import annotations
from typing import Any
from .models import DcteJob


class ImpactAnalyzer:
    def compute(self, job: DcteJob, agg: dict[str, Any]) -> dict[str, Any]:
        transformed = int(agg.get("files_transformed", 0))
        needs_manual = int(agg.get("files_needs_manual", 0))
        failed = int(agg.get("files_failed", 0))

        base_effort = transformed * 0.25 + needs_manual * 2.5 + failed * 4.0

        risk_scores = {"low": 1, "medium": 2, "high": 4}
        risk_total = 0
        for svc in agg.get("services", []):
            r = svc.get("risk_level", "low")
            risk_total += risk_scores.get(r, 1)
        risk_level = "high" if risk_total >= 8 else "medium" if risk_total >= 4 else "low"
        risk_multiplier = {"low": 1.0, "medium": 1.25, "high": 1.6}[risk_level]

        effort_hours = round(base_effort * risk_multiplier, 1)
        raw_complexity = min(
            100.0,
            transformed * 0.5 + needs_manual * 3 + failed * 6 + risk_total * 2,
        )

        return {
            "files_transformed": transformed,
            "files_needs_manual": needs_manual,
            "files_failed": failed,
            "services": len(job.services),
            "risk_level": risk_level,
            "complexity_score": round(raw_complexity, 1),
            "effort_hours": effort_hours,
            "effort_person_days": round(effort_hours / 8.0, 1),
        }
