#!/bin/sh
# PostToolUse hook: run Layer 0 when a SKILL.md is written or edited.
#
# Layer 0 is free and instant, so it can afford to run at edit time -- which is
# the moment a broken file reference or a name/directory mismatch is introduced,
# rather than whenever someone next remembers to check. Layer 1 costs real calls
# and is not run here.
#
# Always exits 0. A linter that can block an edit is a linter that gets removed.

REPO="/Users/aneesh/Documents/skill-regression-harness"

payload=$(cat)
case "$payload" in
  *SKILL.md*) ;;
  *) exit 0 ;;
esac

[ -d "$REPO" ] || exit 0

out=$(cd "$REPO" && python3 run.py --lint-only --out "$REPO/runs/hook-report.html" 2>&1)
errors=$(printf '%s\n' "$out" | grep -E '^[[:space:]]*\[error')

if [ -n "$errors" ]; then
  printf 'skill lint found errors after this edit:\n%s\n' "$errors" >&2
fi

exit 0
