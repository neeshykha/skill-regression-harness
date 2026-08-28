"""Imports claude-eval-kit, falling back to a sibling checkout.

The scoring here is genuinely eval-kit's -- confusion matrices and
confusable-pattern trap auditing -- which makes skill dispatch its second
domain after support triage, and is the point of having extracted it.

The fallback exists because this harness is meant to run unattended on a
schedule. A scheduled run that dies on an import error reports nothing, and a
check that silently stops reporting is worse than no check.
"""

import sys
from pathlib import Path

_SIBLING = Path(__file__).resolve().parent.parent.parent / "claude-eval-kit"

source = "installed package"
try:
    from eval_kit import TrapGroup, audit_traps, confusion_matrix  # noqa: F401
except ModuleNotFoundError:
    if not (_SIBLING / "eval_kit").is_dir():
        raise ModuleNotFoundError(
            "claude-eval-kit not found. Install it with:\n"
            "    pip install -e /path/to/claude-eval-kit\n"
            f"or place a checkout at {_SIBLING}"
        ) from None
    sys.path.insert(0, str(_SIBLING))
    from eval_kit import TrapGroup, audit_traps, confusion_matrix  # noqa: F401

    source = f"sibling checkout at {_SIBLING}"

__all__ = ["TrapGroup", "audit_traps", "confusion_matrix", "source"]
