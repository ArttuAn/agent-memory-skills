#!/usr/bin/env python3
"""Validate every skill in this repo.

The point of a skill library is that its contents are trustworthy on sight:
the code blocks compile, the frontmatter loads in both IDEs, and each skill
carries the sections that make it worth more than the model's own priors.
This runs in CI and takes no network and no API key.

    python3 tools/check_skills.py [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
COMMANDS = ROOT / "commands"
REFERENCES = ROOT / "references"

PREFIX = "memory-"

# Sections every skill must carry. These are the load-bearing ones: the parts
# a frontier model does not produce from a bare spec.
REQUIRED_SECTIONS = [
    "## Use this when",
    "## Workflow",
    "## The decisions that matter",
    "## Failure modes",
    "## Required tests",
    "## Verify",
]

# Every skill must point the reader at the shared contracts rather than
# restating them. A skill that cites nothing has drifted out of the library.
REQUIRED_CITATIONS = ["references/evaluation.md"]

FENCE = re.compile(r"^```(\w+)?[ \t]*$")
# Placeholders are intentional in template code; blank them before compiling.
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9_.\- ]*>")


def code_blocks(text: str, lang: str) -> list[tuple[int, str]]:
    """Return (start_line, source) for each fenced block of the given language."""
    blocks: list[tuple[int, str]] = []
    current: list[str] | None = None
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match and current is None:
            if (match.group(1) or "") == lang:
                current, start = [], number + 1
        elif line.strip() == "```" and current is not None:
            blocks.append((start, "\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def check_python(path: Path, text: str, errors: list[str]) -> int:
    """Every python block must parse. Illustrative fragments opt out with `py`."""
    checked = 0
    for start, source in code_blocks(text, "python"):
        checked += 1
        # `<package>` and friends are template slots, not syntax.
        cleaned = PLACEHOLDER.sub("PLACEHOLDER", source)
        try:
            ast.parse(cleaned)
        except SyntaxError as exc:
            line = start + (exc.lineno or 1) - 1
            errors.append(
                f"{path.relative_to(ROOT)}:{line}: python block does not parse — {exc.msg}"
            )
    return checked


def check_json(path: Path, text: str, errors: list[str]) -> int:
    import json

    checked = 0
    for start, source in code_blocks(text, "json"):
        checked += 1
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            errors.append(
                f"{path.relative_to(ROOT)}:{start + exc.lineno - 1}: json block is invalid — {exc.msg}"
            )
    return checked


def check_frontmatter(path: Path, text: str, errors: list[str]) -> dict[str, str]:
    if not text.startswith("---\n"):
        errors.append(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        errors.append(f"{path.relative_to(ROOT)}: frontmatter is not terminated")
        return {}

    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if line.strip() and not line.startswith((" ", "#")) and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")

    for required in ("name", "description"):
        if not fields.get(required):
            errors.append(f"{path.relative_to(ROOT)}: frontmatter missing '{required}'")

    expected = f"{PREFIX}{path.parent.name}"
    if fields.get("name") != expected:
        errors.append(
            f"{path.relative_to(ROOT)}: frontmatter name '{fields.get('name')}' "
            f"does not match directory (expected '{expected}')"
        )
    if len(fields.get("description", "")) > 220:
        errors.append(f"{path.relative_to(ROOT)}: description is too long to skim (>220 chars)")
    return fields


def check_sections(path: Path, text: str, errors: list[str]) -> None:
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"{path.relative_to(ROOT)}: missing required section '{section}'")


def check_citations(path: Path, text: str, errors: list[str]) -> None:
    for citation in REQUIRED_CITATIONS:
        if citation not in text:
            errors.append(f"{path.relative_to(ROOT)}: never cites '{citation}'")


def check_links(path: Path, text: str, errors: list[str]) -> None:
    """Every references/<file>.md mentioned anywhere must actually exist."""
    for name in set(re.findall(r"references/([a-z0-9-]+\.md)", text)):
        if not (REFERENCES / name).exists():
            errors.append(f"{path.relative_to(ROOT)}: cites missing references/{name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    errors: list[str] = []
    skills = sorted(p for p in SKILLS.glob("*/SKILL.md"))
    if not skills:
        print("no skills found", file=sys.stderr)
        return 1

    total_blocks = 0
    for path in skills:
        text = path.read_text(encoding="utf-8")
        check_frontmatter(path, text, errors)
        check_sections(path, text, errors)
        check_citations(path, text, errors)
        check_links(path, text, errors)
        total_blocks += check_python(path, text, errors)
        total_blocks += check_json(path, text, errors)

        command = COMMANDS / f"{PREFIX}{path.parent.name}.md"
        if not command.exists():
            errors.append(
                f"skills/{path.parent.name}: no matching commands/{PREFIX}{path.parent.name}.md"
            )

    for path in sorted(COMMANDS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        total_blocks += check_python(path, text, errors)
        if not (SKILLS / path.stem.removeprefix(PREFIX) / "SKILL.md").exists():
            errors.append(f"{path.relative_to(ROOT)}: no matching skill directory")

    for path in sorted(REFERENCES.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        total_blocks += check_python(path, text, errors)
        check_links(path, text, errors)

    readme = ROOT / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        check_links(readme, text, errors)
        for path in skills:
            if f"{PREFIX}{path.parent.name}" not in text:
                errors.append(f"README.md: does not list skill '{PREFIX}{path.parent.name}'")

    if errors:
        print(f"FAIL — {len(errors)} problem(s):\n")
        for error in errors:
            print(f"  {error}")
        return 1

    if not args.quiet:
        print(
            f"OK — {len(skills)} skills, {total_blocks} code blocks compiled, "
            f"{len(REQUIRED_SECTIONS)} required sections present in each, "
            f"all reference links resolve"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
