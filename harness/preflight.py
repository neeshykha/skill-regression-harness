"""Checks that must pass before any routing run is believed.

The auth check exists because of a specific, reproduced failure. When the CLI's
OAuth session expires, `claude -p` exits 0 and returns a JSON envelope whose
`subtype` field reads "success" while `is_error` is true and `result` holds the
authentication error. A runner that trusts the exit code records 18 failures; a
runner that trusts `subtype` records 18 confident wrong answers.

Reproduced 2026-08-28 against claude 2.1.220:

    {"is_error": true, ..., "subtype": "success",
     "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
     "terminal_reason": "api_error"}
"""

import json
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Preflight:
    ok: bool
    cli_path: str | None
    cli_version: str | None
    logged_in: bool
    detail: str


def envelope_error(envelope: dict) -> str | None:
    """Returns the error message inside a claude -p JSON envelope, or None.

    Checks `is_error` and `terminal_reason`, never `subtype` and never the
    process exit code. Both of those report success on an expired session.
    """
    if envelope.get("is_error"):
        return str(envelope.get("result") or "unknown error")
    if envelope.get("terminal_reason") == "api_error":
        return str(envelope.get("result") or "api_error")
    return None


def check(timeout: int = 30) -> Preflight:
    cli = shutil.which("claude")
    if not cli:
        return Preflight(False, None, None, False, "claude CLI not found on PATH")

    version = None
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=timeout)
        version = out.stdout.strip() or None
    except Exception as exc:
        return Preflight(False, cli, None, False, f"could not read CLI version: {exc}")

    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=timeout)
        status = json.loads(out.stdout)
    except Exception as exc:
        return Preflight(False, cli, version, False, f"could not read auth status: {exc}")

    if not status.get("loggedIn"):
        return Preflight(
            False,
            cli,
            version,
            False,
            "claude CLI is logged out (`claude auth status` reports loggedIn: false). "
            "Run `claude auth login`. Routing results from a logged-out CLI are not just "
            "missing, they are silently wrong: the error arrives inside a JSON envelope "
            'whose subtype still reads "success".',
        )

    return Preflight(True, cli, version, True, f"ready ({version}, {status.get('authMethod')})")
