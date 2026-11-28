#!/usr/bin/env python3
"""NuOrb: Sicherheitsregeln für Werkzeugaufrufe von Hermes, außerhalb des Sprachmodells (S5 und S6).

Hermes ruft dieses Skript als Shell-Hook auf (config.yaml, hooks.pre_tool_call und hooks.post_tool_call, eingetragen
von first_run.py). Eingabe auf stdin: JSON mit tool_name, tool_input und session_id. Ausgabe auf stdout: nichts
(erlaubt) oder eine Anweisung an Hermes.

S6, fremde Inhalte: Hat der Assistent in einer Sitzung fremde Inhalte gelesen (Websuche, Webseite, Browser, Datei,
Bild, ein vom Nutzer hinzugefügter MCP-Server), wird die Sitzung markiert. Danach braucht jede Aktion mit Wirkung
eine Bestätigung auf der Karte der Oberfläche ({"action": "approve"}). Die Markierung gilt für die ganze Sitzung
(höchstens MARK_TTL), nicht nur für einen Durchgang.

S5, Heimnetz: Shell, Python und Browser dürfen keine privaten, Loopback- oder Link-Local-Ziele ansprechen, außer
den Adressen, die in der Konfiguration stehen (Gehirn, MCP, Smart Home). Das ist eine zweite Schicht, denn diese
Werkzeuge sind im engen Standard ohnehin aus.

Aufruf:  nuorb_policy.py pre|post   (pre mit fail_closed: ein Fehler hier blockiert den Aufruf)
"""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("HERMES_HOME", "/opt/data"))
MARKS = DATA / "nuorb" / "policy" / "fremd"
MARK_TTL = 24 * 3600

# Werkzeuge, deren Ergebnis fremden Text enthalten kann
SOURCE_TOOLS = {"web_search", "web_extract", "read_file", "search_files", "vision_analyze", "session_search"}
# Eigene MCP-Werkzeuge, die nur lesen und deren Ergebnis keine fremden Anweisungen trägt
OWN_READ_MCP = re.compile(r"^mcp__(smarthome__(geraete|zustand|raeume)|wetter__wetter|nuorb__[a-z_]*(lesen|status|info))$")
# Werkzeuge mit Wirkung (nach fremden Inhalten nur mit Bestätigung)
EFFECT_TOOLS = {"terminal", "process_manage", "execute_code", "write_file", "patch", "cronjob_manage", "delegate_task",
                "skill_manage", "memory", "image_generate", "browser_exec", "browser_cdp", "browser_dialog", "browser_vault"}
# Werkzeuge mit eigenem Zeitlimit-Argument, das eine Regel (timeout) nach oben begrenzen kann
TIMEOUT_ARG = {"terminal": ("timeout",), "process_manage": ("timeout",), "execute_code": ("timeout",)}
# Werkzeuge, die Netzwerkziele aus freiem Text ansprechen können (S5)
NET_TOOLS = {"terminal", "process_manage", "execute_code", "browser_exec", "browser_cdp"}

URL_RE = re.compile(r"\b(https?|wss?|ftp)://(?:[^\s/@'\"<>]*@)?(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.\-_]+)(?::(\d{1,5}))?", re.I)
DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}
IP_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
LOCAL_NAMES = re.compile(r"^(localhost|[a-z0-9\-]+\.(local|lan|home|internal|localdomain|home\.arpa|fritz\.box))$", re.I)

# ------------------------------------------------------------------ Regeldatei und Protokoll (v0.7.2)
# rules.json: {"rules": [{"match": "<Werkzeug oder Muster>", "level": "...", "timeout": <s>, "outside": true}, ...]}
# Die erste passende Regel gewinnt. Ohne Datei (oder ohne passende Regel) gelten die eingebauten Standardwerte, genau
# wie in v0.7.1: Werkzeuge mit Wirkung "confirm_after_outside", alles andere "allow".
RULES_FILE = DATA / "nuorb" / "policy" / "rules.json"
LOG_FILE = DATA / "nuorb" / "policy" / "log.jsonl"
LOG_MAX = 5 * 1024 * 1024
LEVELS = ("allow", "confirm", "confirm_after_outside", "deny")
SECRET_KEY = re.compile(r"(token|key|pass|secret|auth|cookie)", re.I)
SECRET_VAL = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]+)")


def parse_rules(raw) -> tuple[list[dict], list[str]]:
    """Gültige Regeln aus dem JSON-Inhalt von rules.json und eine Liste der Fehler (für Protokoll und Admin-Panel)."""
    items = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], ['no list "rules"']
    rules, errors = [], []
    for i, r in enumerate(items):
        if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not r["match"].strip():
            errors.append(f"rule {i + 1}: needs \"match\" (tool name or pattern)")
            continue
        if r.get("level") not in LEVELS:
            errors.append(f"rule {i + 1} ({r['match']}): level must be one of {', '.join(LEVELS)}")
            continue
        t = r.get("timeout")
        if t is not None and not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 3600):
            errors.append(f"rule {i + 1} ({r['match']}): timeout must be 1 to 3600 seconds")
            continue
        if "outside" in r and not isinstance(r["outside"], bool):
            errors.append(f"rule {i + 1} ({r['match']}): outside must be true or false")
            continue
        unknown = set(r) - {"match", "level", "timeout", "outside", "note"}
        if unknown:
            errors.append(f"rule {i + 1} ({r['match']}): unknown fields {', '.join(sorted(unknown))}")
            continue
        rules.append(r)
    return rules, errors


def load_rules() -> tuple[list[dict], str | None]:
    """Gültige Regeln aus rules.json und ein Hinweis für das Protokoll, falls etwas nicht stimmt.
    Fehlende Datei: keine Regeln. Unlesbare Datei: keine Regeln (also die Standardwerte) und ein Hinweis."""
    try:
        raw = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"rules.json unreadable ({exc.__class__.__name__}), built-in defaults active"
    rules, errors = parse_rules(raw)
    return rules, ("; ".join(errors) + " (skipped)" if errors else None)


def rule_for(tool: str, rules: list[dict]) -> dict | None:
    return next((r for r in rules if fnmatch.fnmatchcase(tool, r["match"])), None)


def default_level(tool: str) -> str:
    return "confirm_after_outside" if is_effect(tool) else "allow"


def redact(value):
    if isinstance(value, dict):
        return {k: ("***" if SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return SECRET_VAL.sub("***", value)
    return value


def log_decision(event: str, tool: str, session: str, level: str, result: str, reason: str = "", args=None) -> None:
    """Eine Zeile je Entscheidung. Sitzung nur als Hash, Argumente gekürzt und geschwärzt. Rotation bei LOG_MAX."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".jsonl.1"))
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "tool": tool,
                 "session": hashlib.sha256(session.encode("utf-8", "replace")).hexdigest()[:12] if session else "",
                 "level": level, "result": result, "reason": reason[:300]}
        if args:
            entry["args"] = json.dumps(redact(args), ensure_ascii=False)[:300]
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass   # das Protokoll darf eine Entscheidung nie verhindern


def is_mcp(tool: str) -> bool:
    return tool.startswith("mcp__")


def is_source(tool: str) -> bool:
    if tool in SOURCE_TOOLS or tool.startswith("browser_"):
        return True
    return is_mcp(tool) and not OWN_READ_MCP.match(tool) and not is_effect_mcp_own(tool)


def is_effect_mcp_own(tool: str) -> bool:
    return bool(re.match(r"^mcp__(smarthome_schalten|geraet|nuorb)__", tool))


def is_effect(tool: str) -> bool:
    return tool in EFFECT_TOOLS or (is_mcp(tool) and not OWN_READ_MCP.match(tool))


def mark_file(session: str) -> Path:
    return MARKS / (hashlib.sha256(session.encode("utf-8", "replace")).hexdigest()[:32] + ".json")


def marked(session: str) -> dict | None:
    if not session:
        return None
    try:
        d = json.loads(mark_file(session).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except PermissionError:   # Marken nicht prüfbar: so behandeln, als wäre fremder Inhalt gelesen (ausfallsicher)
        return {"t": time.time(), "tool": "unknown, markers unreadable"}
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and time.time() - float(d.get("t", 0)) < MARK_TTL else None


def set_mark(session: str, tool: str) -> None:
    if not session:
        return
    MARKS.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = mark_file(session)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"t": time.time(), "tool": tool}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    for old in MARKS.glob("*.json"):   # alte Markierungen aufräumen
        try:
            if time.time() - old.stat().st_mtime > MARK_TTL:
                old.unlink()
        except OSError:
            pass


def url_targets(text: str) -> set[tuple[str, int]]:
    """(host, port) jeder URL im Text, Port aus dem Schema, wenn keiner dasteht."""
    return {(h.strip("[]").lower(), int(port) if port else DEFAULT_PORT.get(scheme.lower(), 0))
            for scheme, h, port in URL_RE.findall(text)}


def configured_targets() -> set[tuple[str, int]]:
    """Ziele aus der Hermes-Konfiguration (Gehirn, MCP-URLs) und dem Smart-Home-Zugang: die darf Shell und Browser.
    Verglichen wird Host UND Port, sonst gäbe eine eingetragene 127.0.0.1-Adresse den ganzen Container frei."""
    targets: set[tuple[str, int]] = set()
    for f in (DATA / "config.yaml", DATA / "smarthome.env"):
        try:
            targets |= url_targets(f.read_text(encoding="utf-8"))
        except OSError:
            continue
    return targets


def private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast


@functools.lru_cache(maxsize=64)
def resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    socket.setdefaulttimeout(2)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
        except ValueError:
            continue
    return out


def home_targets(text: str) -> list[str]:
    """Ziele im Heimnetz (oder im Container selbst), die nicht in der Konfiguration stehen."""
    allowed = configured_targets()
    allowed_hosts = {h for h, _ in allowed}
    found = []
    url_hosts = url_targets(text)
    bare = {(ip, 0) for ip in IP_RE.findall(text)} - {(h, 0) for h, _ in url_hosts}
    for host, port in sorted(url_hosts | bare):
        by_name = bool(LOCAL_NAMES.match(host))
        if not by_name and not any(private_ip(ip) for ip in resolve(host)):
            continue
        loopback = host == "localhost" or (not by_name and any(ip.is_loopback for ip in resolve(host)))
        if (host, port) in allowed or (port == 0 and host in allowed_hosts and not loopback):
            continue
        found.append(f"{host}:{port}" if port else host)
    return found


def state_unreadable() -> str | None:
    """Kann der Hook seinen eigenen Zustand (Regeln, Marken) nicht lesen, darf er nicht auf „erlauben“ zurückfallen."""
    for p in (RULES_FILE.parent, MARKS):
        if p.exists() and not os.access(p, os.R_OK | os.X_OK):
            return f"{p.name} not accessible"
    try:
        RULES_FILE.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"rules.json not readable ({exc.__class__.__name__})"
    return None


def decide(event: str, data: dict) -> dict | None:
    tool = str(data.get("tool_name") or "")
    session = str(data.get("session_id") or "")
    raw_args = data.get("tool_input")
    args: dict = raw_args if isinstance(raw_args, dict) else {}
    rules, problem = load_rules()
    if problem:
        log_decision(event, tool, session, "-", "rules-problem", problem)
    rule = rule_for(tool, rules)
    level = rule["level"] if rule else default_level(tool)
    if event == "post":
        if is_source(tool) or (rule and rule.get("outside") is True):
            set_mark(session, tool)
            log_decision(event, tool, session, level, "marked", "outside content read")
        return None
    if level == "deny":
        log_decision(event, tool, session, level, "block", f"rule {rule['match']}" if rule else "", args)
        return {"action": "block", "message": f"NuOrb's tool rules do not allow {tool} here."}
    if tool in NET_TOOLS:
        ziele = home_targets(json.dumps(args, ensure_ascii=False))
        if ziele:
            log_decision(event, tool, session, level, "block", "home network: " + ", ".join(ziele[:3]), args)
            return {"action": "block", "message": "NuOrb blocks access to addresses in the home network from this tool "
                    f"({', '.join(ziele[:3])}). Use the dedicated NuOrb tools instead."}
    broken = state_unreadable()
    if broken and level != "allow":   # ausfallsicher: Aktionen mit Wirkung nur mit Bestätigung
        why = f"NuOrb cannot read its tool rules ({broken}), so this action needs your confirmation."
        log_decision(event, tool, session, level, "confirm", why, args)
        return {"action": "approve", "message": why, "rule_key": f"nuorb-zustand:{tool}:{time.time_ns()}"}
    m = marked(session)
    if level == "confirm" or (level == "confirm_after_outside" and m):
        why = f"After reading outside content ({m.get('tool')}), this action needs your confirmation." if (
            level == "confirm_after_outside" and m) else "NuOrb's tool rules ask for your confirmation for this action."
        log_decision(event, tool, session, level, "confirm", why, args)
        return {"action": "approve", "message": why, "rule_key": f"nuorb-fremd:{tool}:{time.time_ns()}"}
    limit = rule.get("timeout") if rule else None
    if limit and "timeout" in TIMEOUT_ARG.get(tool, ()):
        cur = args.get("timeout")
        if not isinstance(cur, (int, float)) or cur > limit:
            log_decision(event, tool, session, level, "modify", f"timeout capped to {limit} s", args)
            return {"action": "modify", "args": {**args, "timeout": limit}}
    log_decision(event, tool, session, level, "allow", "", args)
    return None


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event not in ("pre", "post"):
        print("usage: nuorb_policy.py pre|post", file=sys.stderr)
        return 2
    data = json.loads(sys.stdin.read() or "{}")
    out = decide(event, data if isinstance(data, dict) else {})
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
