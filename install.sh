#!/usr/bin/env bash
# Install agent-memory-skills into opencode and/or Claude Code.
#
# Usage:
#   ./install.sh                 # global: ~/.config/opencode/skills + ~/.claude/commands
#   ./install.sh --project       # local:   .opencode/skills + .claude/commands in this cwd
#   ./install.sh --opencode-dir "$PWD/.opencode/skills"
#   ./install.sh --claude-dir    "$PWD/.claude/commands"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_SRC="$ROOT/skills"
COMMANDS_SRC="$ROOT/commands"
REFERENCES_SRC="$ROOT/references"

OPENCODE_DEST="${OPENCODE_DEST:-}"
CLAUDE_DEST="${CLAUDE_DEST:-}"
MODE="global"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) MODE="project"; shift ;;
    --opencode-dir) [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { echo "--opencode-dir requires a directory" >&2; exit 2; }; OPENCODE_DEST="$2"; shift 2 ;;
    --claude-dir) [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { echo "--claude-dir requires a directory" >&2; exit 2; }; CLAUDE_DEST="$2"; shift 2 ;;
    --help|-h)
      sed -n '2,9p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" == "project" ]]; then
  OPENCODE_DEST="${OPENCODE_DEST:-.opencode/skills}"
  CLAUDE_DEST="${CLAUDE_DEST:-.claude/commands}"
else
  OPENCODE_DEST="${OPENCODE_DEST:-$HOME/.config/opencode/skills}"
  CLAUDE_DEST="${CLAUDE_DEST:-$HOME/.claude/commands}"
fi

python3 "$ROOT/tools/check_skills.py" --quiet

exec python3 "$ROOT/tools/install_safe.py" \
  --root "$ROOT" \
  --opencode-dir "$OPENCODE_DEST" \
  --claude-dir "$CLAUDE_DEST"
