from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .common import failure, success


def build_skill_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "load_skill",
            "description": "Load specialized knowledge by name.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
    ]


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", path.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(path)}

    def _parse_frontmatter(self, text: str) -> Tuple[Dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta: Dict[str, str] = {}
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        return meta, match.group(2).strip()

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def load_skill(self, name: str) -> Dict[str, Any]:
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys())
            return failure("load_skill", f"Unknown skill '{name}'. Available: {available}")
        return success("load_skill", name=name, content=f"<skill name=\"{name}\">\n{skill['body']}\n</skill>")
