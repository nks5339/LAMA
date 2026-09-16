"""Cross-cutting validators."""
from __future__ import annotations
import re
from pathlib import Path


class BasicValidator:
    def check_java_balance(self, path: Path) -> list[dict]:
        diags: list[dict] = []
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return [{"file": str(path), "level": "error", "message": f"read failed: {e}"}]
        stack = []
        for i, ch in enumerate(src):
            if ch in "{(":
                stack.append((ch, i))
            elif ch in "})":
                if not stack:
                    diags.append({
                        "file": str(path), "level": "warn", "offset": i,
                        "message": f"Unbalanced '{ch}'",
                    })
                else:
                    stack.pop()
        if stack:
            diags.append({
                "file": str(path), "level": "warn",
                "message": f"{len(stack)} unclosed bracket(s)",
            })
        return diags

    def check_sql_semicolons(self, path: Path) -> list[dict]:
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return [{"file": str(path), "level": "error", "message": f"read failed: {e}"}]
        diags: list[dict] = []
        stripped = src.strip()
        if stripped and not (stripped.endswith(";") or stripped.endswith("/")):
            diags.append({
                "file": str(path), "level": "warn",
                "message": "SQL script does not end with ';' or '/'",
            })
        leftovers = re.findall(
            r"\b(VARCHAR2|SYSDATE|NVL|DECODE|DUAL|ROWNUM|MINUS)\b",
            src, re.IGNORECASE,
        )
        if leftovers:
            diags.append({
                "file": str(path), "level": "warn",
                "message": f"Leftover Oracle-isms after translation: "
                           f"{sorted(set(t.upper() for t in leftovers))}",
            })
        return diags
