#!/usr/bin/env python3
"""Copy an installation plan without following links or deleting user trees.

Destinations must be controlled by the installing user; this is not a sandbox
against another process concurrently renaming writable ancestor directories.
"""
import argparse
import os
from pathlib import Path
import tempfile

PREFIX = "memory-"

def reject_links(path):
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError(f"Refusing linked installation path: {part}")
    return path


def file_plan(source, destination):
    source, destination = reject_links(source), reject_links(destination)
    if source.is_file():
        return [(source, destination)]
    if not source.is_dir():
        raise ValueError(f"Not an installation source: {source}")
    plan = []
    for item in sorted(source.iterdir()):
        if item.name == "__pycache__" or item.suffix == ".pyc":
            continue
        plan.extend(file_plan(item, destination / item.name))
    return plan


def install(root, opencode, commands, claude_skills=None):
    root = reject_links(root)
    prefix = PREFIX
    bundles = [root / "references"]
    if prefix == "eu-":
        bundles += [root / "schema", root / "tools"]
    source_dirs = [root / "skills", root / "commands", *bundles]
    plan = []
    for destination in filter(None, [opencode, claude_skills]):
        for skill in sorted((root / "skills").iterdir()):
            target = Path(destination) / (prefix + skill.name)
            plan.extend(file_plan(skill, target))
            for bundle in bundles:
                plan.extend(file_plan(bundle, target / bundle.name))
    plan.extend(file_plan(root / "commands", Path(commands)))

    # Preflight the entire plan before writing even the first file.
    for source, destination in plan:
        target = destination.resolve()
        if any(target == base.resolve() or base.resolve() in target.parents
               or target in base.resolve().parents for base in source_dirs):
            raise ValueError("Installation destination overlaps repository sources")
        reject_links(destination)
        if destination.exists() and not destination.is_file():
            raise ValueError(f"Refusing to replace a non-file: {destination}")
        if any(p.exists() and not p.is_dir() for p in destination.parents):
            raise ValueError(f"Installation parent is not a directory: {destination}")

    for source, destination in plan:
        reject_links(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Replace the directory entry, never truncate a possibly hard-linked file.
        fd, temporary = tempfile.mkstemp(prefix=".skill-install-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(source.read_bytes())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print(f"Installed {len(plan)} files; unrelated destination files were preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--opencode-dir", required=True, type=Path)
    parser.add_argument("--claude-dir", required=True, type=Path)
    parser.add_argument("--claude-skills-dir", type=Path)
    args = parser.parse_args()
    try:
        install(args.root, args.opencode_dir, args.claude_dir, args.claude_skills_dir)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Installation refused: {exc}\n")


if __name__ == "__main__":
    main()
