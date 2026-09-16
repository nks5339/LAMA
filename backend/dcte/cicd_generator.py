"""CI/CD pipeline artefact generator."""
from __future__ import annotations
from pathlib import Path


TEMPLATES_DIR = Path(__file__).parent / "templates" / "cicd"


class CicdGenerator:
    def write(self, provider: str, output_root: Path) -> Path:
        provider = provider.strip().lower()
        cicd_dir = output_root / "cicd"
        cicd_dir.mkdir(parents=True, exist_ok=True)

        if provider in ("github", "github-actions"):
            tpl = (TEMPLATES_DIR / "github-actions.yml").read_text(encoding="utf-8")
            out = cicd_dir / "github-actions.yml"
        elif provider in ("azure", "azure-devops"):
            tpl = (TEMPLATES_DIR / "azure-pipelines.yml").read_text(encoding="utf-8")
            out = cicd_dir / "azure-pipelines.yml"
        elif provider == "jenkins":
            tpl = (TEMPLATES_DIR / "Jenkinsfile").read_text(encoding="utf-8")
            out = cicd_dir / "Jenkinsfile"
        else:
            raise ValueError(f"Unknown CI/CD provider: {provider}")

        out.write_text(tpl, encoding="utf-8")
        return out
