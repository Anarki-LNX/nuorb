"""NuOrb-Server (Docker): gemeinsame Bausteine für UI- und Admin-Hülle.

- Pfade im Container
- Zertifikate (eigene Heim-CA + Server-Zertifikat, oder eigene Dateien)
- Kopplung: Einmal-Codes (Admin erzeugt), Geräte-Tokens (UI prüft)

Geheimnisse liegen nur als SHA-256 auf der Platte (Codes und Tokens), Dateien mit Modus 600.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import ipaddress
import json
import contextvars
import os
import re
import secrets
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ Pfade
DATA = Path(os.environ.get("HERMES_HOME", "/opt/data"))          # Hermes-Daten (Volume)
J = DATA / "nuorb"                                              # alles von NuOrb-Server
CONF = J / "config"                                              # ui.json, internal.token, devices.json, pairing.json
CERTS = J / "certs"
MODELS = Path(os.environ.get("NUORB_MODELS", "/opt/models"))    # Stimmen + STT-Modelle (Volume)
APP = Path(__file__).resolve().parent
DEVICES = CONF / "devices.json"
PAIRING = CONF / "pairing.json"

# ------------------------------------------------------------------ getrennte Benutzer (v0.7.4)
# Admin-Panel und Oberfläche laufen als „nuorb“, Hermes und damit die Werkzeuge des Sprachmodells als „hermes“.
# Die Geheimnisse von NuOrb (J ohne die Ausnahmen unten) gehören nur nuorb. Was nuorb im Bereich von Hermes schreibt,
# geht an hermes zurück, damit sich für Hermes nichts ändert. Hermes darf genau diese NuOrb-Dateien lesen:
NUORB_USER, HERMES_USER = "nuorb", "hermes"
HERMES_READS = ("mcp.token", "weather.json", "weather.env", "ui.json")   # in CONF: nuorb:hermes 0640 (ui.json: Stimme für die Spracherkennung, keine Geheimnisse)
# unter J, aber (auch) von Hermes-Prozessen geschrieben. Gehören immer hermes: im Sticky-Ordner J darf hermes nur eigene
# Dateien ersetzen. models-status.json schreiben beide (Spracherkennung als hermes, Stimmen-Download aus dem Panel als nuorb).
HERMES_AREAS_IN_J = ("policy", "cache", "logs/stt", "models-status.json")
RECLAIM_SKIP = ("installs", "cache")                                  # Laufzeiten und Zwischenspeicher von Hermes, groß


def user_ids(name: str) -> tuple[int, int] | None:
    import pwd
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def separated() -> bool:
    """True, wenn dieser Prozess als nuorb läuft (Image ab v0.7.4). In Tests und alten Images: False."""
    ids = user_ids(NUORB_USER)
    return ids is not None and os.geteuid() == ids[0]


def in_nuorb_area(p: Path) -> bool:
    """Gehört p zu den Dateien von nuorb (Geheimnisse, Einstellungen), nicht zum Bereich von Hermes?"""
    try:
        rel = Path(os.path.abspath(p)).relative_to(J)
    except ValueError:
        return False
    return not any(rel == Path(a) or Path(a) in rel.parents for a in HERMES_AREAS_IN_J)


def fix_owner(p: Path, target: Path | None = None) -> None:
    """Beim Schreiben als nuorb: Bereich von Hermes -> hermes, die Lesedateien für Hermes -> nuorb:hermes 0640.
    target: Ziel, wenn p die Zwischendatei vor dem Umbenennen ist (die Regel richtet sich nach dem Ziel). So stimmt der
    Besitz schon im Moment des Umbenennens. Ohne Trennung (Tests, alte Images) passiert nichts. Fehler werden ignoriert."""
    if not separated():
        return
    h = user_ids(HERMES_USER)
    if not h:
        return
    t = target or p
    with contextlib.suppress(OSError):
        if in_nuorb_area(t):
            if t.parent == CONF and t.name in HERMES_READS:
                os.chown(p, os.geteuid(), h[1])
                os.chmod(p, 0o640)
            return
        os.chown(p, h[0], h[1], follow_symlinks=False)


def admin_hooks(adm) -> None:
    """Hooks von NuOrb-Admin (server.py) für getrennte Benutzer setzen: im Panel und in first_run gleich."""
    adm.PREPARE_TMP = fix_owner
    adm.AFTER_WRITE = fix_owner
    adm.AFTER_HERMES = reclaim_hermes_files
    adm.ENV_MODE = 0o640


def reclaim_hermes_files(root: Path = DATA) -> int:
    """Alles, was nuorb im Bereich von Hermes angelegt hat (etwa über die Hermes-CLI), an hermes zurückgeben.
    Überspringt den NuOrb-Bereich und die großen Ordner von Hermes. Gibt die Zahl der geänderten Einträge zurück."""
    if not separated():
        return 0
    me, h = os.geteuid(), user_ids(HERMES_USER)
    if not h:
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [x for x in dirnames if not (d == root and x in RECLAIM_SKIP) and not (d / x).is_symlink()
                       and (not in_nuorb_area(d / x) or (d / x) == J)]
        for name in [*dirnames, *filenames]:
            f = d / name
            if f == J or in_nuorb_area(f):
                continue
            with contextlib.suppress(OSError):
                if os.lstat(f).st_uid == me:
                    os.chown(f, h[0], h[1], follow_symlinks=False)
                    n += 1
    return n

# ------------------------------------------------------------------ Werkzeuge von Hermes (S5/S6)
# Enger Standard für die Plattform api_server (NuOrb-Oberfläche und App): was ein Sprachassistent braucht.
# Alle aktiven MCP-Server kommen von Hermes automatisch dazu. „extended“ = alle Werkzeuge des API-Servers.
TOOLS_FILE = CONF / "tools.json"
TOOLSETS = {"narrow": ["web", "vision", "skills", "cronjob", "todo", "memory", "session_search"],
            "extended": ["hermes-api-server"]}
POLICY_CMD = "/opt/nuorb/venv/bin/python -I /opt/nuorb/app/nuorb_policy.py"
POLICY_PRE = r".*"   # jeder Aufruf: Regeln aus rules.json können jedes Werkzeug betreffen (v0.7.2)
POLICY_POST = r"^(web_search|web_extract|read_file|search_files|vision_analyze|browser_.*|mcp__.*)$"


POLICY_DIR = J / "policy"
POLICY_RULES = POLICY_DIR / "rules.json"
POLICY_LOG = POLICY_DIR / "log.jsonl"
POLICY_TEMPLATE = {
    "_info": "NuOrb tool rules. The first matching rule wins. Without a matching rule: tools with an effect need a "
             "confirmation after outside content was read, everything else is allowed. "
             "level: allow | confirm | confirm_after_outside | deny. timeout (seconds) caps shell and Python. "
             "outside: true marks the conversation like a web page does.",
    "rules": [],
    "examples": [
        {"match": "terminal", "level": "confirm", "timeout": 60},
        {"match": "mcp__smarthome_schalten__*", "level": "confirm"},
        {"match": "mcp__myserver__*", "level": "confirm_after_outside", "outside": True},
        {"match": "image_generate", "level": "deny"},
    ],
}


def ensure_policy_rules() -> bool:
    """Leere Regeldatei mit Beispielen anlegen, wenn es keine gibt (leere Liste = Standardwerte wie v0.7.1)."""
    if POLICY_RULES.exists():
        return False
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(POLICY_DIR, 0o700)
    _write_json(POLICY_RULES, POLICY_TEMPLATE)
    return True


def tools_state() -> dict:
    """{"mode": "narrow"|"extended", "notice": bool}. Ohne Datei: enger Standard."""
    try:
        d = json.loads(TOOLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    mode = d.get("mode") if d.get("mode") in TOOLSETS else "narrow"
    return {"mode": mode, "notice": bool(d.get("notice"))}


def apply_tools_policy(data, mode: str) -> bool:
    """Werkzeug-Liste und Policy-Hooks in die Hermes-Konfiguration (ruamel-Daten aus load_config_rt) schreiben.
    Eigene Hooks des Nutzers bleiben. Gibt True zurück, wenn sich etwas geändert hat."""
    changed = False
    pt = data.get("platform_toolsets")
    if not isinstance(pt, dict):
        data["platform_toolsets"] = pt = {}
    if list(pt.get("api_server") or []) != TOOLSETS[mode]:
        pt["api_server"] = list(TOOLSETS[mode])
        changed = True
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        data["hooks"] = hooks = {}
    for event, arg, matcher, extra in (("pre_tool_call", "pre", POLICY_PRE, {"fail_closed": True}),
                                       ("post_tool_call", "post", POLICY_POST, {})):
        want = {"matcher": matcher, "command": f"{POLICY_CMD} {arg}", "timeout": 10, **extra}
        cur = [h for h in (hooks.get(event) or []) if isinstance(h, dict)]
        others = [h for h in cur if "nuorb_policy.py" not in str(h.get("command", ""))]
        mine = [h for h in cur if "nuorb_policy.py" in str(h.get("command", ""))]
        if [dict(h) for h in mine] != [want]:
            hooks[event] = [want] + others
            changed = True
    return changed


def allow_policy_hooks(hermes_home: Path = DATA) -> None:
    """Die beiden NuOrb-Hooks in Hermes' Hook-Allowlist eintragen (statt hooks_auto_accept für alle Hooks)."""
    f = hermes_home / "shell-hooks-allowlist.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    approvals = [e for e in (d.get("approvals") or []) if isinstance(e, dict)]
    want = [("pre_tool_call", f"{POLICY_CMD} pre"), ("post_tool_call", f"{POLICY_CMD} post")]
    if all(any(e.get("event") == ev and e.get("command") == cmd for e in approvals) for ev, cmd in want):
        return
    approvals = [e for e in approvals if "nuorb_policy.py" not in str(e.get("command", ""))]
    approvals += [{"event": ev, "command": cmd, "approved_at": now_iso(), "script_mtime_at_approval": None} for ev, cmd in want]
    d["approvals"] = approvals
    _write_json(f, d)

PRODUCT = "nuorb"
API_VERSION = 1
CODE_TTL = 600            # Kopplungscode: 10 Minuten, einmalig
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O/1/I


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def lang() -> str:
    """Sprache: im Admin gewählt (ui.json "lang"), sonst NUORB_LANG aus der .env."""
    v = _read_json(CONF / "ui.json", {}).get("lang") if (CONF / "ui.json").exists() else None
    if v not in ("de", "en"):
        v = "en" if env("NUORB_LANG", "de").lower().startswith("en") else "de"
    return v


# Sprache des Admin-Webinterfaces (Einrichtung, Koppeln, Fehlermeldungen); getrennt von der Sprache des Assistenten.
REQ_LANG: contextvars.ContextVar[str | None] = contextvars.ContextVar("nuorb_req_lang", default=None)


def ui_lang() -> str:
    """Kopf X-UI-Lang der laufenden Anfrage (Wahl im Browser), sonst ADMIN_LANG/UI_LANG, Standard Englisch."""
    v = REQ_LANG.get()
    if v in ("de", "en"):
        return v
    return "de" if (env("ADMIN_LANG") or env("UI_LANG", "en")).lower().startswith("de") else "en"


def ui_lang_setting() -> str:
    """Admin-Sprache für den Browser: de | en | auto (auto = Browsersprache).
    Reihenfolge: ui.json ui_lang (im Admin gewählt), ADMIN_LANG (Image-Standard en), UI_LANG."""
    v = str(ui_cfg().get("ui_lang") or "").lower()
    if v not in ("de", "en"):
        v = (env("ADMIN_LANG") or env("UI_LANG", "auto")).lower()
    return v if v in ("de", "en") else "auto"


def server_label() -> str:
    """Sichtbarer Servername mit Version, z. B. „NuOrb Server v0.2.1“ (nur mit der Marke)."""
    ver = env("NUORB_BUILD").lstrip("v")
    return f"{brand()} Server" + (f" v{ver}" if ver and ver != "dev" else (" (dev)" if ver else ""))


def version() -> str:
    """Version dieses Images ohne „v“ (NUORB_BUILD beim Bauen), z. B. „0.7.3“, sonst „dev“."""
    return env("NUORB_BUILD").lstrip("v") or "dev"


def version_tuple(v: str) -> tuple[int, int, int] | None:
    """„v0.7.3“ oder „0.7.3“ -> (0, 7, 3). Alles andere (dev, test, Vorabversionen) -> None."""
    m = re.fullmatch(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})", (v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def tr(de: str, en: str) -> str:
    return de if ui_lang() == "de" else en


DEFAULT_VOICE = {"de": "de_DE-thorsten-medium", "en": "en_US-joe-medium"}
# Stimmen, die der Assistent zur Auswahl anbietet (Piper, rhasspy/piper-voices). Nur Stimmen, deren Daten UND
# Basismodell CC0/CC-BY sind (Lizenzkette siehe THIRD_PARTY_LICENSES.md).
VOICE_CHOICES = {
    "de": [("de_DE-thorsten-medium", "Thorsten (male, CC0)")],
    "en": [("en_US-joe-medium", "Joe (US, male, CC0)")],
}
# Namensnennung für die Seite „Über“ der Verwaltung (Lizenzkette: Aufnahmen + Basisstimme, siehe MODEL_CARD).
VOICE_CREDITS = {
    "de_DE-thorsten-medium": {"name": "Thorsten", "license": "CC0-1.0",
                              "data": "Thorsten-Voice by Thorsten Müller", "data_url": "https://www.thorsten-voice.de",
                              "base": "rdh (CC0)"},
    "en_US-joe-medium": {"name": "Joe", "license": "CC0-1.0",
                         "data": "OHF-Voice/voice-datasets", "data_url": "https://github.com/OHF-Voice/voice-datasets",
                         "base": "rdh (CC0)"},
}
STT_CREDIT = {"name": "NVIDIA Parakeet-TDT-0.6B-v3", "license": "CC-BY-4.0",
              "url": "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
              "license_url": "https://creativecommons.org/licenses/by/4.0/",
              "conversion": "int8 ONNX export by k2-fsa/sherpa-onnx", "conversion_url": "https://github.com/k2-fsa/sherpa-onnx",
              "text": "Speech recognition: NVIDIA Parakeet-TDT-0.6B-v3 (CC-BY-4.0), ONNX export by k2-fsa/sherpa-onnx"}
# Nicht für kommerzielle Nutzung freigegeben (Forschungs-/NC-Daten oder -Basismodell): werden ersetzt und gelöscht.
# lessac (Blizzard 2013, nur Forschung) und alles darauf Aufbauende, ryan (CC-BY-NC-SA) und Abgeleitete, amy (unklar).
RESTRICTED_VOICE = re.compile(r"^(en_GB-alan-.*|en_US-ryan-.*|en_US-amy-.*|en_US-lessac-.*|en_US-libritts_r-.*"
                              r"|de_DE-kerstin-low|de_DE-thorsten-high)$")


def voice_replacement(name: str) -> str | None:
    """Ersatz für eine nicht freigegebene Stimme (EN → Joe, sonst Thorsten), None wenn die Stimme in Ordnung ist."""
    if not isinstance(name, str) or not RESTRICTED_VOICE.match(name):
        return None
    return DEFAULT_VOICE["en"] if name.startswith("en_") else DEFAULT_VOICE["de"]


def voice_name() -> str:
    v = _read_json(CONF / "ui.json", {}).get("piper_voice") if (CONF / "ui.json").exists() else None
    v = v if isinstance(v, str) and v else (env("NUORB_VOICE") or DEFAULT_VOICE[lang()])
    return voice_replacement(v) or v


MODELS_STATUS = J / "models-status.json"


def models_status() -> dict:
    return _read_json(MODELS_STATUS, {})


def set_models_status(**kw) -> None:
    """Fortschritt der Modell-Downloads für Einrichtung und Panel. Nur eine Anzeige: ein Fehler beim Schreiben darf den
    Download nie abbrechen (v0.7.4: PermissionError im Sticky-Ordner brach die Erstinstallation ab)."""
    try:
        d = models_status()
        d.update(kw, time=now_iso())
        _write_json(MODELS_STATUS, d)
        os.chmod(MODELS_STATUS, 0o644)
    except OSError as exc:
        print(f"[nuorb] models-status.json not written ({exc.__class__.__name__}: {exc})", file=sys.stderr, flush=True)


def ui_cfg() -> dict:
    return _read_json(CONF / "ui.json", {}) if (CONF / "ui.json").exists() else {}


def brand() -> str:
    """Produktname (ui.json brand_name vor BRAND_NAME), Standard NuOrb."""
    b = ui_cfg().get("brand_name")
    return (b if isinstance(b, str) and b.strip() else env("BRAND_NAME", "NuOrb") or "NuOrb").strip()[:40]


def assistant_name() -> str:
    """Name, mit dem man den Assistenten anspricht (ui.json assistant_name), Standard = Marke."""
    a = ui_cfg().get("assistant_name")
    if isinstance(a, str) and a.strip():
        return a.strip()[:40]
    return env("ASSISTANT_NAME")[:40] or brand()


def user_name() -> str:
    u = ui_cfg().get("user_name")
    return u.strip()[:40] if isinstance(u, str) else ""


def onboarded() -> bool:
    return ui_cfg().get("onboarded") is True


def server_name() -> str:
    return env("NUORB_NAME") [:40] or assistant_name()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@contextlib.contextmanager
def json_lock(p: Path):
    """Sperre über Prozessgrenzen (Oberfläche und Verwaltung schreiben dieselben Dateien): Lesen, Ändern und Schreiben
    von devices.json und pairing.json als ein Schritt. Sperrdatei <datei>.lock daneben, 0600."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_json(p: Path, default):
    with contextlib.suppress(FileNotFoundError, ValueError):
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        fix_owner(tmp, p)   # Besitz vor dem Umbenennen (siehe fix_owner)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    fix_owner(p)


DEFAULT_NETS = "127.0.0.0/8,::1/128,10.0.0.0/8,192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10"


def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """IPv4-Standardgateway (im Container: der Docker-Gateway), None wenn unbekannt."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path(route_file).read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return str(ipaddress.IPv4Address(bytes.fromhex(f[2])[::-1]))
    return None


def allowed_nets() -> list:
    """Heimnetz, VPN und Link-Local, dazu der eigene Docker-Gateway (nicht das ganze 172.16.0.0/12): unter dieser
    Adresse erscheinen Clients, wenn Docker die Ports per Proxy weiterreicht. NUORB_ALLOWED_NETS ersetzt die Liste."""
    raw = env("NUORB_ALLOWED_NETS")
    if not raw:
        gw = default_gateway()
        raw = DEFAULT_NETS + (f",{gw}/32" if gw else "")
    return [ipaddress.ip_network(n.strip(), strict=False) for n in raw.split(",") if n.strip()]


def proxy_kwargs() -> dict:
    """Reverse-Proxy (NUORB_TRUSTED_PROXY, Komma-Liste): nur dann X-Forwarded-For auswerten, und nur von diesen Adressen.
    Dann sehen Allowlist und Sperre die echte Client-Adresse statt der des Proxys."""
    p = env("NUORB_TRUSTED_PROXY")
    return {"proxy_headers": True, "forwarded_allow_ips": p} if p else {"proxy_headers": False}


def is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address((host or "").split("%")[0]).is_loopback
    except ValueError:
        return False


# ------------------------------------------------------------------ Zertifikate
def hosts() -> list[str]:
    """Namen/IPs, unter denen der Server erreichbar ist (NUORB_HOSTS, kommagetrennt)."""
    out = []
    for h in env("NUORB_HOSTS").replace(" ", ",").split(","):
        h = h.strip().strip("[]")
        if h and h not in out:
            out.append(h)
    return out


def tls_mode() -> str:
    """selfsigned (Standard) | custom (eigene Dateien) | off (TLS macht ein Proxy davor, z. B. Caddy)"""
    m = env("NUORB_TLS", "selfsigned").lower()
    return m if m in ("selfsigned", "custom", "off") else "selfsigned"


def cert_paths() -> tuple[Path, Path]:
    if tls_mode() == "custom":
        return Path(env("NUORB_TLS_CERT", "/certs/fullchain.pem")), Path(env("NUORB_TLS_KEY", "/certs/privkey.pem"))
    return CERTS / "server.crt", CERTS / "server.key"


# Namensbeschränkung der Heim-CA (NameConstraints): Eine auf dem Smartphone installierte CA soll nur für das Heimnetz
# gelten, nicht für jede Domain. Erlaubt: Heimnetz-Endungen, localhost, die DNS-Namen aus NUORB_HOSTS beim Anlegen der
# CA und private IP-Bereiche. Bestehende CAs bleiben unverändert (sonst müssten alle Geräte neu koppeln).
CA_DNS_SUFFIXES = ("local", "lan", "home", "home.arpa", "internal", "localdomain", "fritz.box", "localhost", "nuorb")
CA_IP_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
              "::1/128", "fc00::/7", "fe80::/10")


def ca_name_constraints(names: list[str]):
    from cryptography import x509
    dns = list(CA_DNS_SUFFIXES)
    for h in names:
        try:
            ipaddress.ip_address(h)
        except ValueError:
            h = h.lower().rstrip(".")
            if h and h not in dns and not any(h == d or h.endswith("." + d) for d in CA_DNS_SUFFIXES):
                dns.append(h)
    return x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_network(n)) for n in CA_IP_NETS],
        excluded_subtrees=None)


def ca_covers(ca_crt, name: str) -> bool:
    """Deckt die Namensbeschränkung der CA diesen Namen ab? CAs ohne Beschränkung decken alles ab."""
    from cryptography import x509
    try:
        nc = ca_crt.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return True
    try:
        ip = ipaddress.ip_address(name)
        nets = [g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.IPAddress)]
        return any(isinstance(n, (ipaddress.IPv4Network, ipaddress.IPv6Network)) and ip in n for n in nets)
    except ValueError:
        n = name.lower().rstrip(".")
        return any(isinstance(g, x509.DNSName) and (n == g.value or n.endswith("." + g.value)) for g in nc.permitted_subtrees or [])


def ensure_certs() -> None:
    """Heim-CA einmalig, Server-Zertifikat neu bei geänderten Namen oder < 30 Tage Restlaufzeit."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    CERTS.mkdir(parents=True, exist_ok=True)
    os.chmod(CERTS, 0o700)
    ca_key_p, ca_crt_p = CERTS / "nuorb-ca.key", CERTS / "nuorb-ca.crt"
    now = dt.datetime.now(dt.timezone.utc)

    def save_key(k, p):
        p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        os.chmod(p, 0o600)

    if not (ca_key_p.exists() and ca_crt_p.exists()):
        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{brand()} Home CA {secrets.token_hex(3)}"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"{brand()} (self-hosted)")])
        crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
               .not_valid_after(now + dt.timedelta(days=3650))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
               .add_extension(ca_name_constraints(hosts()), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
               .sign(k, hashes.SHA256()))
        save_key(k, ca_key_p)
        ca_crt_p.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
        os.chmod(ca_crt_p, 0o644)
        print(f"[nuorb] New home CA created, fingerprint {ca_fingerprint()}", flush=True)
    if tls_mode() != "selfsigned":
        return
    want_all = sorted(set(hosts() + ["localhost", "127.0.0.1", "nuorb"]))
    ca_crt = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
    # Nur Namen, die die CA abdeckt: ein einziger nicht erlaubter Name macht das ganze Zertifikat ungültig (RFC 5280)
    nicht = [h for h in want_all if not ca_covers(ca_crt, h)]
    want = [h for h in want_all if h not in nicht]
    srv_crt_p, srv_key_p = CERTS / "server.crt", CERTS / "server.key"
    with contextlib.suppress(OSError, ValueError, x509.ExtensionNotFound):   # fehlt oder beschädigt: neu ausstellen
        cur = x509.load_pem_x509_certificate(srv_crt_p.read_bytes())
        san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = sorted({str(v) for v in san.get_values_for_type(x509.DNSName)}
                      | {str(v) for v in san.get_values_for_type(x509.IPAddress)})
        if have == want and cur.not_valid_after_utc - now > dt.timedelta(days=30) and srv_key_p.exists():
            return
    ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey), "Heim-CA mit EC-Schlüssel erwartet"
    k = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for h in want:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            alt.append(x509.DNSName(h))
    if nicht:
        print(f"[certs] WARNING: the home CA does not cover {', '.join(nicht)} (name constraints set when it was created). "
              "Apps and browsers will reject the certificate for these names. Use a home network name or IP, "
              "or start with a new CA (delete certs/ in the data volume, then pair all devices again).", flush=True)
    crt = (x509.CertificateBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, next((h for h in hosts() if h in want), "localhost"))]))
           .issuer_name(ca_crt.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=825))
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .add_extension(x509.SubjectAlternativeName(alt), critical=False)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .sign(ca_key, hashes.SHA256()))
    save_key(k, srv_key_p)
    srv_crt_p.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    os.chmod(srv_crt_p, 0o644)
    print(f"[nuorb] Server certificate created for {', '.join(want)} (825 days).", flush=True)


def ca_fingerprint() -> str:
    """SHA-256 über das CA-Zertifikat (DER), 64 Hex-Zeichen groß. Leer, wenn keine eigene CA im Spiel ist."""
    if tls_mode() != "selfsigned":
        return ""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    try:
        crt = x509.load_pem_x509_certificate((CERTS / "nuorb-ca.crt").read_bytes())
    except OSError:
        return ""
    return hashlib.sha256(crt.public_bytes(serialization.Encoding.DER)).hexdigest().upper()


def cert_names() -> list[str]:
    from cryptography import x509
    try:
        crt = x509.load_pem_x509_certificate(cert_paths()[0].read_bytes())
        san = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
               [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except (OSError, ValueError, x509.ExtensionNotFound):
        return []


def ssl_kwargs() -> dict:
    if tls_mode() == "off":
        return {}
    c, k = cert_paths()
    return {"ssl_certfile": str(c), "ssl_keyfile": str(k)}


# ------------------------------------------------------------------ Kopplung
def _norm_code(c: str) -> str:
    return "".join(ch for ch in str(c).upper() if ch in CODE_ALPHABET)


def new_pairing_code(created_by: str) -> dict:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    exp = time.time() + CODE_TTL
    with json_lock(PAIRING):
        data = {k: v for k, v in _read_json(PAIRING, {}).items() if v.get("exp", 0) > time.time()}
        data[_sha(code)] = {"exp": exp, "by": created_by[:40], "created": now_iso()}
        _write_json(PAIRING, data)
    return {"code": f"{code[:4]}-{code[4:]}", "expires": int(exp), "ttl": CODE_TTL}


def redeem_pairing_code(code: str) -> bool:
    c = _norm_code(code)
    if len(c) != 8:
        return False
    h = _sha(c)
    with json_lock(PAIRING):  # einmalig, auch bei gleichzeitigen Anfragen
        data = _read_json(PAIRING, {})
        hit = None
        for k, v in data.items():
            if hmac.compare_digest(k, h) and v.get("exp", 0) > time.time():
                hit = k
        if not hit:
            return False
        data.pop(hit, None)
        _write_json(PAIRING, {k: v for k, v in data.items() if v.get("exp", 0) > time.time()})
    return True


def devices() -> dict:
    return _read_json(DEVICES, {})


def add_device(name: str, platform: str, ip: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    did = "d_" + secrets.token_hex(6)
    with json_lock(DEVICES):
        d = devices()
        d[did] = {"name": (name or "Device")[:60], "platform": (platform or "unknown")[:20], "token": _sha(token),
                  "created": now_iso(), "last_seen": now_iso(), "ip": ip}
        _write_json(DEVICES, d)
    return did, token


_seen_cache: dict[str, float] = {}


def device_for_token(token: str, ip: str = "") -> str | None:
    if not token or len(token) > 200:
        return None
    h = _sha(token)
    for did, v in devices().items():
        if hmac.compare_digest(v.get("token", ""), h):
            if time.time() - _seen_cache.get(did, 0) > 60:  # „zuletzt gesehen“ höchstens einmal pro Minute schreiben
                _seen_cache[did] = time.time()
                with json_lock(DEVICES):  # frisch lesen: ein gleichzeitiger Widerruf darf nicht überschrieben werden
                    d = devices()
                    if did not in d:
                        return None
                    d[did]["last_seen"], d[did]["ip"] = now_iso(), ip
                    _write_json(DEVICES, d)
            return did
    return None


def revoke_device(did: str) -> bool:
    with json_lock(DEVICES):
        d = devices()
        if did not in d:
            return False
        d.pop(did)
        _write_json(DEVICES, d)
    return True


def pairing_required() -> bool:
    return env("NUORB_REQUIRE_PAIRING", "1") != "0"


# ------------------------------------------------------------------ Lokales Gehirn: Server-Erkennung (docs/llm-backends.md §1.4)
def llm_detect(base: str, api_key: str = "", timeout: float = 5) -> dict:
    """Erkennt den Server hinter einer OpenAI-kompatiblen Adresse (nur Anzeige, nie Pflicht).
    -> {"server": Name, "models": [ids], "ctx": int|None, "ok": bool, "error": str}"""
    import urllib.request
    base = base.rstrip("/").removesuffix("/v1")
    hdr = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def get(path: str):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=hdr), timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (OSError, ValueError):   # nicht erreichbar, Zeitüberschreitung, keine JSON-Antwort
            return 0, {}
    st, mj = get("/v1/models")
    out = {"server": "OpenAI-kompatibel", "models": [], "ctx": None, "ok": st == 200, "error": ""}
    if st in (401, 403):
        out["error"] = "api_key"
        return out
    if st != 200:
        out["error"] = "unreachable" if st == 0 else f"http {st}"
        return out
    data = mj.get("data") or []
    out["models"] = [d.get("id") for d in data if isinstance(d, dict) and d.get("id")]
    owners = {str(d.get("owned_by", "")) for d in data if isinstance(d, dict)}
    if "library" in owners or (get("/api/version")[1].get("version") and "models" in get("/api/tags")[1]):
        out["server"] = "Ollama"
    elif "llamacpp" in owners or "default_generation_settings" in get("/props")[1]:
        out["server"] = "llama.cpp"
        out["ctx"] = (get("/props")[1].get("default_generation_settings") or {}).get("n_ctx")
    elif "vllm" in owners or get("/version")[1].get("version"):
        out["server"] = "vLLM"
        out["ctx"] = next((d.get("max_model_len") for d in data if isinstance(d, dict) and d.get("max_model_len")), None)
    elif "organization_owner" in owners or get("/api/v0/models")[0] == 200:
        out["server"] = "LM Studio"
        # nur Sprachmodelle (keine Embeddings), geladene zuerst; Kontext = loaded_context_length der geladenen
        v = get("/api/v0/models")[1].get("data")
        if isinstance(v, list):
            llms = [d for d in v if isinstance(d, dict) and d.get("id") and d.get("type", "llm") in ("llm", "vlm")]
            loaded = [d for d in llms if d.get("state") == "loaded" and isinstance(d.get("loaded_context_length"), int)]
            out["models"] = [d["id"] for d in loaded] + [d["id"] for d in llms if d not in loaded] or out["models"]
            out["ctx"] = min((d["loaded_context_length"] for d in loaded), default=None)
    elif get("/readyz")[0] == 200:
        out["server"] = "LocalAI"
    return out