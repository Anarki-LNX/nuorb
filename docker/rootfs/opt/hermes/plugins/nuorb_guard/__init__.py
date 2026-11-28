"""NuOrb-Wache für Werkzeugaufrufe (v0.7.4).

NuOrbs Regeln für Werkzeuge (Bestätigungen, Heimnetz-Sperre, fremde Inhalte) laufen als Shell-Hook von Hermes
(``nuorb_policy.py pre``). Hermes registriert Shell-Hooks erst beim Öffnen einer Sitzung aus config.yaml und
shell-hooks-allowlist.json. Fehlt dort etwas oder ist eine Datei in dem Moment nicht lesbar, überspringt Hermes den
Hook mit einer Warnung und führt Werkzeuge ohne Prüfung aus. Diese Wache ist ein gebündeltes Plugin (immer geladen,
unabhängig von config.yaml) und lässt einen Aufruf nur zu, wenn der NuOrb-Hook in diesem Prozess registriert ist.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)
POLICY_PRE = "/opt/nuorb/venv/bin/python -I /opt/nuorb/app/nuorb_policy.py pre"
MESSAGE = ("NuOrb: tools are locked because NuOrb's safety rules (policy hook) are not active in this Hermes process. "
           "Nothing was run. Restart Hermes in the admin panel (Services) or restart the container.")


def policy_hook_active() -> bool:
    try:
        from agent import shell_hooks
    except Exception:   # Hermes ohne Shell-Hooks: dann gibt es auch keinen NuOrb-Hook
        return False
    with shell_hooks._registered_lock:
        return any(k[1] == "pre_tool_call" and k[3] == POLICY_PRE for k in shell_hooks._registered)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict[str, str]]:
    if policy_hook_active():
        return None
    logger.error("[nuorb-guard] blocked tool call %s: NuOrb policy hook is not registered in this process", tool_name)
    return {"action": "block", "message": MESSAGE}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
