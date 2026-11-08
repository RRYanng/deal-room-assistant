"""Skills: each v2 specialist keeps its instructions in skills/<name>/SKILL.md.

A SKILL.md has a short front matter (name, description, inputs, outputs, tools), an overview, and one
"## " section per prompt. The program reads a section as a system prompt and fills in `{placeholders}`
from code, so lists such as the claim keys exist in one place only.
"""

import re

from app.config import PROJECT_ROOT

SKILLS_DIR = PROJECT_ROOT / "skills"
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def load_skill(name: str) -> dict:
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    meta, body = {}, text
    found = _FRONT_MATTER.match(text)
    if found:
        body = text[found.end():]
        for line in found.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    sections, title = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            sections[title] = []
        elif title:
            sections[title].append(line)
    return {"meta": meta, "sections": {t: "\n".join(lines).strip() for t, lines in sections.items()}}


def skill_prompt(name: str, section: str, **values: str) -> str:
    """One section of a skill as a prompt, with `{placeholder}` replaced by `values`."""
    prompt = load_skill(name)["sections"][section]
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt
