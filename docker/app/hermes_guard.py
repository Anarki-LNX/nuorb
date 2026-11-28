"""NuOrb-Server: Prüfung vor dem Start des Hermes-Gateways (v0.7.4, gegen „fail-open“).

Liest Hermes seine config.yaml nicht (Rechte, kaputte Datei) oder fehlen dort NuOrbs Policy-Hooks, startet Hermes mit
Standardeinstellungen: alle Werkzeuge, keine Bestätigungen. Das darf nie passieren. Darum prüft dieses Skript als
hermes, bevor das Gateway startet (Wrapper /opt/nuorb/guard/hermes):
  - config.yaml ist lesbar und gültiges YAML,
  - hooks.pre_tool_call enthält den NuOrb-Hook mit fail_closed, hooks.post_tool_call den NuOrb-Hook,
  - beide stehen in Hermes' Hook-Allowlist (sonst laufen sie ohne Rückfrage nicht),
  - platform_toolsets.api_server ist der enge Standard oder „extended“ (bewusst im Panel gewählt).
Exit 0 = starten. Sonst Grund auf stderr, Marke für den Healthcheck, Exit 1 (der Wrapper wartet und s6 versucht es neu).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DATA = Path(os.environ.get("HERMES_HOME", "/opt/data"))
MARK = Path(os.environ.get("NUORB_GUARD_MARK", "/run/nuorb-guard/blocked"))
POLICY_CMD = "/opt/nuorb/venv/bin/python -I /opt/nuorb/app/nuorb_policy.py"
TOOLSETS = ({"web", "vision", "skills", "cronjob", "todo", "memory", "session_search"}, {"hermes-api-server"})


def problems(data_dir: Path = DATA) -> list[str]:
    try:
        import yaml  # type: ignore[import-untyped]
        cfg = yaml.safe_load((data_dir / "config.yaml").read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"config.yaml is not readable ({exc.__class__.__name__}: {exc.strerror or exc})"]
    except Exception as exc:   # YAML-Fehler
        return [f"config.yaml is not valid YAML ({exc.__class__.__name__})"]
    if not isinstance(cfg, dict):
        return ["config.yaml is empty or not a mapping"]
    out = []
    h = cfg.get("hooks")
    hooks: dict = h if isinstance(h, dict) else {}
    pre = [h for h in hooks.get("pre_tool_call") or [] if isinstance(h, dict) and h.get("command") == f"{POLICY_CMD} pre"]
    post = [h for h in hooks.get("post_tool_call") or [] if isinstance(h, dict) and h.get("command") == f"{POLICY_CMD} post"]
    if not pre or pre[0].get("fail_closed") is not True:
        out.append("NuOrb policy hook pre_tool_call (fail_closed) is missing")
    if not post:
        out.append("NuOrb policy hook post_tool_call is missing")
    try:
        allow = json.loads((data_dir / "shell-hooks-allowlist.json").read_text(encoding="utf-8")).get("approvals") or []
    except (OSError, ValueError, AttributeError):
        allow = []
    for ev, arg in (("pre_tool_call", "pre"), ("post_tool_call", "post")):
        if not any(isinstance(e, dict) and e.get("event") == ev and e.get("command") == f"{POLICY_CMD} {arg}" for e in allow):
            out.append(f"NuOrb policy hook {arg} is not in shell-hooks-allowlist.json")
    p = cfg.get("platform_toolsets")
    pt: dict = p if isinstance(p, dict) else {}
    if set(pt.get("api_server") or []) not in TOOLSETS:
        out.append(f"platform_toolsets.api_server is neither the narrow default nor extended: {pt.get('api_server')!r}")
    return out


def main() -> int:
    found = problems()
    if not found:
        try:
            MARK.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    msg = "; ".join(found)
    print(f"[nuorb-guard] NOT starting the Hermes gateway: {msg}. Hermes would otherwise run on default settings "
          "without NuOrb's tool rules. Restart the container (nuorb-init repairs owners and hooks) or check the file.",
          file=sys.stderr, flush=True)
    try:
        MARK.parent.mkdir(parents=True, exist_ok=True)
        MARK.write_text(msg + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
