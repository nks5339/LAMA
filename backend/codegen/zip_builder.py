"""ZIP packaging for generated codebase. Pure stdlib."""
import io
import zipfile
from typing import List, Dict, Any


def build_zip(project_name: str, files: List[Dict[str, Any]]) -> bytes:
    repo_name = (project_name or "lama-generated").lower().replace(" ", "-").replace("_", "-")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            path = f.get("file_path") or "unknown.txt"
            content = f.get("content") or ""
            zf.writestr(f"{repo_name}/{path}", content)
    buf.seek(0)
    return buf.read()


