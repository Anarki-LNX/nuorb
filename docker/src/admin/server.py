"""NuOrb-Verwaltung: Einstellungs-Web-UI für Hermes/NuOrb (HTTPS :8444, nur LAN, Login).

Grundsaetze
- Jede Dateiaenderung läuft über denselben Weg: Vorschau (Diff) -> Bestätigung -> Sicherung -> atomar schreiben
  -> Prüfung (z. B. `hermes config check`). Jede Sicherung lässt sich wieder einspielen (ebenfalls mit Vorschau).
- Secrets (.env, Schlüssel in config.yaml) verlassen den Server nie im Klartext: nur "gesetzt …1234".
- Login: Passwort (scrypt), das der Nutzer beim ersten Aufruf mit einem Einmal-Code selbst setzt.
- Dienst-Neustarts nur über den root-Helfer nuorb-admin-ctl (systemd-Path-Unit, feste Whitelist).
- Smartphone nur über einen eigenen SSH-Schlüssel mit command=-Restriktion auf dem Smartphone.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import time
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

# ------------------------------------------------------------------ Pfade und Einstellungen
HOME = Path(os.environ.get("NUORB_HOME", "/home/nuorb"))
BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ADMIN_DATA", str(BASE / "data")))
BACKUPS = DATA / "backups"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes")))
HERMES_BIN = os.environ.get("HERMES_BIN", str(HOME / ".local/bin/hermes"))
HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
STT_URL = os.environ.get("STT_HEALTH_URL", "http://127.0.0.1:8178/health")
UI_URL = os.environ.get("NUORB_UI_URL", "https://127.0.0.1:8443")
UI_STATIC = Path(os.environ.get("NUORB_UI_STATIC", str(HOME / "nuorb-ui/static")))
UI_CERTS = Path(os.environ.get("ADMIN_CERTS", str(HOME / "certs/nuorb-admin")))  # eigenes Zertifikat (von nuorb-ui ausgestellt)
UI_SETTINGS = Path(os.environ.get("NUORB_UI_SETTINGS", str(HOME / ".config/nuorb/ui.json")))
OLLAMA_ENV = os.environ.get("OLLAMA_URL", "")  # nur Startwert, wenn config.yaml noch keine Ollama-Adresse hat
OLLAMA_PROVIDER = "pc-ollama"  # Eintrag unter providers: in config.yaml (Hermes: provider custom:pc-ollama)
CTL_DIR = Path(os.environ.get("ADMIN_CTL_DIR", "/var/lib/nuorb-admin-ctl"))
PHONE_HOST = os.environ.get("PHONE_HOST", "")  # Kiosk-Geraet (Xiaomi); leer = keins (Kachel/Menue ausgeblendet)
KIOSK_PAUSED = os.environ.get("KIOSK_PAUSED", "").lower() in ("1", "true", "yes")  # pausiert: neutral, nicht pruefen
PHONE_ONLINE_S = int(os.environ.get("PHONE_ONLINE_S", "300"))  # gekoppelte App gilt so lange nach last_seen als aktiv
GPU_LABEL = os.environ.get("ADMIN_GPU_LABEL", "")
# Produktwerte (im Image per Env fest, hier nur Anzeige/Standard)
BRAND_NAME = os.environ.get("BRAND_NAME", "NuOrb")  # Produktname (Titel, Login)
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "") or BRAND_NAME  # wie der Assistent heisst
WAKE_PHRASE = os.environ.get("WAKE_PHRASE", "") or "Hey " + (ASSISTANT_NAME.capitalize() if ASSISTANT_NAME.isupper() else ASSISTANT_NAME)
UI_LANG = os.environ.get("ADMIN_LANG", "") or os.environ.get("UI_LANG", "")  # de | en | leer/auto = Browser
SERVER_VERSION = os.environ.get("NUORB_BUILD", "")
SERVER_LABEL = f"{BRAND_NAME} Server" + (f" v{SERVER_VERSION.lstrip('v')}" if SERVER_VERSION else "")
NAME_RE = r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9 .-]{0,23}"  # z. B. „RX 7800 XT · 16 GB“, leer = keine Angabe
PHONE_USER = os.environ.get("PHONE_USER", "nuorb-ctl")
PHONE_KEY = Path(os.environ.get("PHONE_KEY", str(BASE / "keys/handy_ed25519")))
HOST = os.environ.get("ADMIN_HOST", "0.0.0.0")
PORT = int(os.environ.get("ADMIN_PORT", "8444"))
SESSION_HOURS = 12        # gleitend: so lange ohne Nutzung gültig
SESSION_MAX_DAYS = 7      # höchstens so lange ab Anmeldung, auch bei ständiger Nutzung (gestohlenes Cookie läuft aus)
MAX_SESSIONS = 20         # gleichzeitige Sitzungen; die älteste fällt weg
UNITS = {  # Anzeige-Name -> systemd-Unit (Whitelist, identisch im root-Helfer)
    "hermes": "hermes-gateway.service",
    "stt": "nuorb-stt-router.service",
    "ui": "nuorb-ui.service",
    "admin": "nuorb-admin.service",
}
UNIT_LABELS = {"hermes": "Hermes-Gateway (Gehirn-Anbindung)", "stt": "Spracherkennung (Parakeet)",
               "ui": "NUORB-Oberfläche (Smartphone)", "admin": "Verwaltung (diese Seite)"}
ALLOWED_NETS = [ipaddress.ip_network(n) for n in os.environ.get(
    "ADMIN_ALLOWED_NETS", "127.0.0.0/8,::1/128,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12").split(",")]

SECRET_KEY_RE = re.compile(r"(key|token|secret|passw|credential|auth_header)", re.I)
ENV_PROTECTED = {"API_SERVER_KEY", "API_SERVER_ENABLED", "API_SERVER_HOST", "API_SERVER_PORT"}

FILES = {  # Schlüssel -> (Pfad, Art)
    "config": (HERMES_HOME / "config.yaml", "yaml"),
    "soul": (HERMES_HOME / "SOUL.md", "text"),
    "memory": (HERMES_HOME / "memories/MEMORY.md", "text"),
    "user": (HERMES_HOME / "memories/USER.md", "text"),
    "env": (HERMES_HOME / ".env", "env"),
    "ui": (UI_SETTINGS, "json"),
    "smarthome": (HERMES_HOME / "smarthome.env", "env"),
}
FILE_LABELS = {"config": "Hermes-Konfiguration", "soul": "Persönlichkeit (SOUL.md)", "memory": "Gedächtnis (MEMORY.md)",
               "user": "Nutzerprofil (USER.md)", "env": "Schlüssel (.env)", "ui": "Smartphone-Oberfläche (ui.json)",
               "smarthome": "Smart-Home-Zugang (smarthome.env)"}

DATA.mkdir(parents=True, exist_ok=True)
BACKUPS.mkdir(parents=True, exist_ok=True)
os.chmod(DATA, 0o700)

yaml_rt = YAML()
yaml_rt.preserve_quotes = True
yaml_rt.width = 4096
yaml_rt.indent(mapping=2, sequence=4, offset=2)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_field(v: object) -> str:
    """Ein Feld des Audit-Logs: Tabs und Zeilenumbrüche aus Nutzereingaben (Gerätenamen, Zeitplannamen …) dürfen
    keine neuen Spalten oder gefälschten Zeilen erzeugen. Backslash zuerst, damit sich das umkehren lässt."""
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def audit(who: str, what: str) -> None:
    with open(DATA / "audit.log", "a", encoding="utf-8") as f:
        f.write(f"{now_iso()}\t{_log_field(who)}\t{_log_field(what)}\n")


# Haken für die Docker-Hülle (getrennte Benutzer, v0.7.4): nach jedem Schreiben bzw. jedem Aufruf der Hermes-CLI
# Besitz richtigstellen. Ohne Docker bleiben beide leer.
AFTER_WRITE = None    # Callable[[Path], None]
PREPARE_TMP = None    # Callable[[Path, Path], None]: (Zwischendatei, Ziel) vor dem Umbenennen, z. B. Besitzer setzen
ENV_MODE = 0o600      # Docker ab v0.7.4: 0o640 (Gruppe hermes, die Oberfläche liest ihren Schlüssel ohne Sonderrechte)
AFTER_HERMES = None   # Callable[[], object]


def atomic_write(path: Path, data: str | bytes, mode: int | None = None) -> None:
    new_parent = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if new_parent and AFTER_WRITE:
        AFTER_WRITE(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    if isinstance(data, str):
        data = data.encode("utf-8")
    old_mode = None
    with contextlib.suppress(FileNotFoundError):
        old_mode = path.stat().st_mode & 0o777
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode if mode is not None else (old_mode if old_mode is not None else 0o644))
    if PREPARE_TMP:   # Besitz schon vor dem Umbenennen: das Ziel ist nie kurz für den falschen Benutzer
        PREPARE_TMP(tmp, path)
    os.replace(tmp, path)
    if AFTER_WRITE:
        AFTER_WRITE(path)


# ------------------------------------------------------------------ Secrets maskieren
def mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return ""
    if len(v) < 12:
        return "gesetzt"
    return f"gesetzt …{v[-4:]}"


TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), re.compile(r"\b[a-f0-9]{32,}\b"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"),
]


def redact_text(s: str) -> str:
    for p in TOKEN_PATTERNS:
        s = p.sub(lambda m: (m.group(1) if m.groups() else "") + "‹verborgen›", s)
    for v in env_secret_values():
        if len(v) >= 8:
            s = s.replace(v, "‹verborgen›")
    return s


def redact_tree(o):
    if isinstance(o, dict):
        return {k: (mask(v) if SECRET_KEY_RE.search(str(k)) and isinstance(v, str) and v else redact_tree(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [redact_tree(x) for x in o]
    return o


# ------------------------------------------------------------------ .env lesen (nur serverseitig)
def read_env() -> list[tuple[str, str | None, str]]:
    """Liste (Name, Wert, Rohzeile); Kommentare/Leerzeilen mit Name None."""
    path = FILES["env"][0]
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m and not line.lstrip().startswith("#"):
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out.append((m.group(1), val, line))
        else:
            out.append((None, None, line))
    return out


def env_value(name: str) -> str:
    for n, v, _ in read_env():
        if n == name:
            return v or ""
    return os.environ.get(name, "")


def env_secret_values() -> list[str]:
    return [v for n, v, _ in read_env() if n and v and SECRET_KEY_RE.search(n)]


def env_is_secret(name: str) -> bool:
    return bool(SECRET_KEY_RE.search(name))


def mask_env_text(text: str, extra: frozenset = frozenset()) -> str:
    """.env-Inhalt für Diffs: Werte geheimer Variablen (und extra) nur maskiert."""
    lines = []
    for line in text.splitlines():
        m = re.match(r"^(\s*(?:# aus: )?(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(.*)$", line)  # auch abgeschaltete Smart-Home-Zeilen
        if m and (not line.lstrip().startswith("#") or line.startswith("# aus: ")) and (env_is_secret(m.group(2)) or m.group(2) in extra):
            val = m.group(3).strip().strip("\"'")
            lines.append(f"{m.group(1)}{mask(val)}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


# ------------------------------------------------------------------ Login
AUTH_FILE = DATA / "auth.json"
SESS_FILE = DATA / "sessions.json"
SETUP_FILE = DATA / "setup-code.txt"
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, list[float]] = {}


def load_sessions() -> None:
    with contextlib.suppress(OSError, ValueError):
        SESSIONS.update(json.loads(SESS_FILE.read_text()))


def save_sessions() -> None:
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
        SESSIONS.pop(k, None)
    atomic_write(SESS_FILE, json.dumps(SESSIONS), 0o600)


def auth_configured() -> bool:
    return AUTH_FILE.exists()


def ensure_setup_code() -> None:
    if auth_configured():
        with contextlib.suppress(FileNotFoundError):
            SETUP_FILE.unlink()
        return
    if not SETUP_FILE.exists():
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        atomic_write(SETUP_FILE, code + "\n", 0o600)
    print(f"[nuorb-admin] Noch kein Passwort gesetzt. Einrichtungscode liegt in {SETUP_FILE} (nur für nuorb/root lesbar).",
          flush=True)


def hash_pw(pw: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return {"alg": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(h).decode(), "set": now_iso()}


def check_pw(pw: str) -> bool:
    try:
        a = json.loads(AUTH_FILE.read_text())
        salt = base64.b64decode(a["salt"])
        h = hashlib.scrypt(pw.encode(), salt=salt, n=a["n"], r=a["r"], p=a["p"], maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(h, base64.b64decode(a["hash"]))
    except (OSError, KeyError, TypeError, ValueError):   # fehlt, beschädigt oder unbekanntes Format: kein Login
        return False


def pw_problem(pw: str) -> str | None:
    if len(pw) < 10:
        return "Das Passwort braucht mindestens 10 Zeichen."
    if len(pw) > 256:
        return "Das Passwort ist zu lang."
    if len(set(pw)) < 5:
        return "Das Passwort ist zu einfach."
    return None


# Passwortprüfung (scrypt, ~100 ms und 32 MB) nicht im Event-Loop: in einem Thread, höchstens zwei gleichzeitig,
# pro Adresse nur eine. Der Versuch zählt schon VOR dem Hash, sonst kämen viele parallele Anfragen an der Sperre vorbei.
PW_SLOTS = asyncio.Semaphore(2)
PW_BUSY: set[str] = set()


async def pw_attempt(ip: str, pw: str) -> bool:
    """True = Passwort stimmt. Zählt den Versuch vorab als Fehlversuch und nimmt ihn bei Erfolg zurück."""
    if ip in PW_BUSY:
        raise HTTPException(429, "Bitte warten, die vorige Anmeldung läuft noch.")
    if (w := rate_limited(ip)) > 0:
        raise HTTPException(429, f"Zu viele Fehlversuche. Bitte {w} s warten.")
    PW_BUSY.add(ip)
    stamp = time.time()
    FAILS.setdefault(ip, []).append(stamp)
    try:
        async with PW_SLOTS:
            ok = await asyncio.to_thread(check_pw, pw)
    finally:
        PW_BUSY.discard(ip)
    if ok:
        with contextlib.suppress(ValueError):
            FAILS.get(ip, []).remove(stamp)
    return ok


def rate_limited(ip: str) -> int:
    now = time.time()
    arr = [t for t in FAILS.get(ip, []) if now - t < 900]
    FAILS[ip] = arr
    if len(arr) >= 5:
        return int(min(900, 30 * 2 ** (len(arr) - 5)) - (now - arr[-1]))
    return 0


def new_session(ip: str) -> str:
    tok = secrets.token_urlsafe(32)
    for k in sorted(SESSIONS, key=lambda k: SESSIONS[k].get("start", 0))[:max(0, len(SESSIONS) - MAX_SESSIONS + 1)]:
        SESSIONS.pop(k, None)
    SESSIONS[hashlib.sha256(tok.encode()).hexdigest()] = {"exp": time.time() + SESSION_HOURS * 3600, "ip": ip,
                                                           "created": now_iso(), "start": time.time()}
    save_sessions()
    return tok


def session_of(request: Request) -> dict | None:
    tok = request.cookies.get("nuorb_sess", "")
    if not tok:
        return None
    key = hashlib.sha256(tok.encode()).hexdigest()
    s = SESSIONS.get(key)
    if not s or s["exp"] < time.time():
        return None
    start = s.setdefault("start", time.time())   # Sitzungen von vor v0.7.2: Höchstdauer ab jetzt
    if time.time() - start > SESSION_MAX_DAYS * 86400:
        SESSIONS.pop(key, None)
        return None
    s["exp"] = min(max(s["exp"], time.time() + SESSION_HOURS * 3600 - 600), start + SESSION_MAX_DAYS * 86400)  # gleitend
    return s


def set_cookie(resp: Response, tok: str) -> None:
    resp.set_cookie("nuorb_sess", tok, max_age=SESSION_HOURS * 3600, httponly=True, secure=True, samesite="strict", path="/")


# Schutz vor DNS-Rebinding: nur Hostnamen, die eine fremde Webseite nicht auf diesen Server umbiegen kann. Erlaubt sind
# IP-Adressen, Namen ohne Punkt (nur im Heimnetz auflösbar), Heimnetz-Endungen, der eigene Hostname und alles aus
# NUORB_HOSTS und NUORB_ALLOWED_HOSTS (Komma-Liste, auch *.beispiel.de). Gilt unabhängig von TLS und Kopplung.
HOME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".fritz.box")
ALLOWED_HOSTS = {h.strip().lower().rstrip(".") for v in ("NUORB_HOSTS", "NUORB_ALLOWED_HOSTS")
                 for h in os.environ.get(v, "").split(",") if h.strip()} | {"localhost", socket.gethostname().lower()}


def host_ok(request: Request) -> bool:
    h = (request.headers.get("host") or "").strip().lower()
    if not h:
        return True  # ohne Host-Kopf (HTTP/1.0, Werkzeuge): kein Browser, kein Rebinding
    if h.startswith("["):
        name = h[1:h.find("]")] if "]" in h else h
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    name = name.rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name in ALLOWED_HOSTS or name.endswith(HOME_SUFFIXES):
        return True
    return any(a.startswith("*.") and name.endswith(a[1:]) for a in ALLOWED_HOSTS)


# ------------------------------------------------------------------ App + Middleware
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC = {"/", "/api/auth/state", "/api/auth/login", "/api/auth/setup", "/favicon.ico", "/nuorb-ca.crt"}


@app.middleware("http")
async def guard(request: Request, call_next):
    ip = request.client.host if request.client else "0.0.0.0"
    try:
        ok = any(ipaddress.ip_address(ip.split("%")[0]) in n for n in ALLOWED_NETS)
    except ValueError:
        ok = False
    if not ok:
        return PlainTextResponse("Nur im Heimnetz erreichbar.", status_code=403)
    if not host_ok(request):
        print(f"[host] {request.client.host if request.client else '?'}: Host {request.headers.get('host', '')[:80]!r} abgelehnt", flush=True)
        return JSONResponse({"detail": "Unbekannter Hostname. Eigene Namen in NUORB_ALLOWED_HOSTS eintragen."}, status_code=421)
    path = request.url.path
    if request.method not in ("GET", "HEAD") and not path.startswith("/internal/"):
        # CSRF: eigener Header (nicht per Formular setzbar) + Origin muss passen
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if request.headers.get("x-nuorb") != "1" or (origin and origin.split("://", 1)[-1] != host):
            return JSONResponse({"detail": "Anfrage abgelehnt (CSRF-Schutz)."}, status_code=403)
    if path.startswith("/internal/"):
        # Server-zu-Server (nuorb-ui): nur Loopback + eigenes Token, keine Session/CSRF, nur Gehirn-Modus
        if ip not in ("127.0.0.1", "::1") or not internal_token_ok(request.headers.get("x-nuorb-internal", ""), path):
            return JSONResponse({"detail": "Nicht erlaubt."}, status_code=403)
        return await call_next(request)
    public = path in PUBLIC or path.startswith("/static/") or path.startswith("/ui/")
    if not public and session_of(request) is None:
        return JSONResponse({"detail": "Bitte anmelden."}, status_code=401)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                               "script-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; "
                                               "base-uri 'none'; form-action 'self'")
    resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def who(request: Request) -> str:
    return request.client.host if request.client else "?"


async def body(request: Request) -> dict:
    try:
        d = await request.json()
    except ValueError:
        raise HTTPException(400, "Ungültige Anfrage.") from None
    if not isinstance(d, dict):
        raise HTTPException(400, "Ungültige Anfrage.")
    return d


# ------------------------------------------------------------------ Auth-Endpunkte
def ui_brand() -> dict:
    """Marke, Name, Weckwort und Sprache wie nuorb-ui: ui.json vor Umgebung. Oeffentlich (Login-Seite)."""
    ui = read_ui_settings()

    def pick(k, d):
        v = ui.get(k)
        return (v if isinstance(v, str) and v.strip() else d).strip()[:24]
    lang = pick("ui_lang", UI_LANG or "auto").lower()
    return {"brand_name": pick("brand_name", BRAND_NAME), "assistant_name": pick("assistant_name", ASSISTANT_NAME),
            "wake_phrase": pick("wake_phrase", WAKE_PHRASE), "ui_lang": lang if lang in ("de", "en") else "auto"}


@app.get("/api/auth/state")
async def auth_state(request: Request):
    b = ui_brand()
    return {"configured": auth_configured(), "loggedIn": session_of(request) is not None,
            "brand": b["brand_name"], "ui_lang": b["ui_lang"],  # flach fuer die Docker-Seiten
            "ui": {"brand": b["brand_name"], "assistant_name": b["assistant_name"], "wake_phrase": b["wake_phrase"],
                   "ui_lang": b["ui_lang"], "server": SERVER_LABEL},
            "setupHint": None if auth_configured() else
            f"cat {SETUP_FILE}"}


@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    d = await body(request)
    ip = who(request)
    if auth_configured():
        raise HTTPException(409, "Das Passwort ist bereits gesetzt.")
    if (w := rate_limited(ip)) > 0:
        raise HTTPException(429, f"Zu viele Versuche. Bitte {w} s warten.")
    try:
        code = SETUP_FILE.read_text().strip()
    except FileNotFoundError:
        raise HTTPException(500, "Kein Einrichtungscode vorhanden. Dienst neu starten.")
    given = str(d.get("code", "")).strip().upper()
    if not hmac.compare_digest(given, code):
        FAILS.setdefault(ip, []).append(time.time())
        audit(ip, "setup: falscher Einrichtungscode")
        raise HTTPException(403, "Der Einrichtungscode stimmt nicht.")
    pw = str(d.get("password", ""))
    if (p := pw_problem(pw)):
        raise HTTPException(400, p)
    atomic_write(AUTH_FILE, json.dumps(hash_pw(pw)), 0o600)
    SETUP_FILE.unlink(missing_ok=True)
    audit(ip, "setup: Passwort gesetzt")
    resp = JSONResponse({"ok": True})
    set_cookie(resp, new_session(ip))
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request):
    d = await body(request)
    ip = who(request)
    if (w := rate_limited(ip)) > 0:
        raise HTTPException(429, f"Zu viele Fehlversuche. Bitte {w} s warten.")
    if not auth_configured():
        raise HTTPException(409, "Noch kein Passwort gesetzt.")
    if not await pw_attempt(ip, str(d.get("password", ""))):
        audit(ip, "login: falsches Passwort")
        await asyncio.sleep(0.8)
        raise HTTPException(403, "Passwort falsch.")
    FAILS.pop(ip, None)
    audit(ip, "login")
    resp = JSONResponse({"ok": True})
    set_cookie(resp, new_session(ip))
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    tok = request.cookies.get("nuorb_sess", "")
    SESSIONS.pop(hashlib.sha256(tok.encode()).hexdigest(), None)
    save_sessions()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("nuorb_sess", path="/")
    return resp


@app.post("/api/auth/password")
async def auth_password(request: Request):
    d = await body(request)
    if not await pw_attempt(who(request), str(d.get("old", ""))):
        raise HTTPException(403, "Das bisherige Passwort stimmt nicht.")
    pw = str(d.get("new", ""))
    if (p := pw_problem(pw)):
        raise HTTPException(400, p)
    atomic_write(AUTH_FILE, json.dumps(hash_pw(pw)), 0o600)
    SESSIONS.clear()
    ip = who(request)
    audit(ip, "Passwort geändert, alle Sitzungen abgemeldet")
    resp = JSONResponse({"ok": True})
    set_cookie(resp, new_session(ip))
    return resp


@app.post("/api/auth/logout-all")
async def auth_logout_all(request: Request):
    SESSIONS.clear()
    save_sessions()
    audit(who(request), "alle Sitzungen abgemeldet")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("nuorb_sess", path="/")
    return resp


# ------------------------------------------------------------------ Änderungen: Vorschau -> Bestätigung -> Sicherung
PENDING: dict[str, dict] = {}


def file_path(key: str) -> tuple[Path, str]:
    if key in FILES:
        return FILES[key]
    m = re.fullmatch(r"skill:([a-z0-9][a-z0-9_\-]*)/([a-z0-9][a-z0-9_\-]*)", key)
    if m:
        return HERMES_HOME / "skills" / m.group(1) / m.group(2) / "SKILL.md", "skill"
    raise HTTPException(400, "Unbekannte Datei.")


def label_of(key: str) -> str:
    if key.startswith("skill:"):
        return f"Skill {key[6:]}"
    return FILE_LABELS.get(key, key)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def validate_content(kind: str, text: str) -> None:
    if kind == "yaml":
        try:
            yaml_rt.load(text)
        except YAMLError as e:
            raise HTTPException(400, f"YAML ist ungültig: {e}")
    elif kind == "json":
        try:
            json.loads(text)
        except ValueError as e:
            raise HTTPException(400, f"JSON ist ungültig: {e}")
    elif kind == "skill":
        fm = parse_frontmatter(text)
        if not fm.get("name") or not fm.get("description"):
            raise HTTPException(400, "Ein Skill braucht im Kopf (---) mindestens name und description.")


def make_preview(key: str, new: str | None, desc: str, effect: str = "", danger: str = "") -> dict:
    """new=None bedeutet: Datei löschen (nur Skills)."""
    path, kind = file_path(key)
    old = read_text(path)
    if new is not None:
        if new == old:
            raise HTTPException(409, "Keine Änderung: Der Inhalt ist bereits so gespeichert.")
        validate_content(kind, new)
    show = (lambda s: mask_env_text(s, SH_MASK)) if key == "smarthome" else mask_env_text if kind == "env" else (lambda s: s)
    nl = lambda s: s if (not s or s.endswith("\n")) else s + "\n"
    diff = "".join(difflib.unified_diff(nl(show(old)).splitlines(True), nl(show(new or "")).splitlines(True),
                                        fromfile=f"{path.name} (jetzt)", tofile=f"{path.name} (neu)", n=2))
    if kind != "env":
        diff = redact_secret_yaml_lines(diff) if kind == "yaml" else diff
    pid = secrets.token_urlsafe(12)
    PENDING[pid] = {"key": key, "old_sha": sha(old), "new": new, "desc": desc, "exp": time.time() + 900,
                    "exists": path.exists()}
    for k in [k for k, v in PENDING.items() if v["exp"] < time.time()]:
        PENDING.pop(k, None)
    return {"preview": pid, "file": label_of(key), "path": str(path), "diff": diff or "(nur Leerzeichen/Format)",
            "desc": desc, "effect": effect, "danger": danger}


def redact_secret_yaml_lines(diff: str) -> str:
    out = []
    for line in diff.splitlines(True):
        m = re.match(r"^([+\- ]\s*)([A-Za-z0-9_\-]+)(\s*:\s*)(.+?)(\s*)$", line)
        if m and SECRET_KEY_RE.search(m.group(2)) and m.group(4).strip() not in ("", "null", "''", '""', "{}"):
            val = m.group(4).strip().strip("\"'")
            out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{mask(val)}\n")
        else:
            out.append(line)
    return "".join(out)


def backup(key: str, text: str, reason: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = key.replace(":", "__").replace("/", "--")
    bid = f"{safe}@{ts}-{secrets.token_hex(2)}"
    d = BACKUPS / bid
    d.mkdir(parents=True)
    atomic_write(d / "content", text, 0o600)
    atomic_write(d / "meta.json", json.dumps({"key": key, "time": now_iso(), "reason": reason,
                                              "existed": text != ""}), 0o600)
    return bid


async def run_hermes(*args: str, timeout: float = 90) -> tuple[int, str]:
    env = {"HOME": str(HOME), "PATH": f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "de_DE.UTF-8",
           "NO_COLOR": "1", "TERM": "dumb", "HERMES_HOME": str(HERMES_HOME)}
    try:
        p = await asyncio.create_subprocess_exec(HERMES_BIN, *args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
                                                 env=env, cwd=str(HOME))
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.decode("utf-8", "replace"))
        if AFTER_HERMES:
            AFTER_HERMES()
        return p.returncode or 0, redact_text(text)[-6000:]
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        return 124, f"Zeitüberschreitung nach {timeout:.0f} s"
    except FileNotFoundError:
        return 127, "hermes nicht gefunden"


def restart_hint(key: str) -> str:
    if key in ("config", "soul", "memory", "user", "env") or key.startswith("skill:"):
        return "hermes"
    if key == "ui":
        return ""  # nuorb-ui liest ui.json bei jeder Anfrage neu
    return ""


@app.post("/api/change/{pid}/apply")
async def change_apply(pid: str, request: Request):
    p = PENDING.pop(pid, None)
    if not p or p["exp"] < time.time():
        raise HTTPException(410, "Die Vorschau ist abgelaufen. Bitte erneut speichern.")
    key = p["key"]
    path, kind = file_path(key)
    old = read_text(path)
    if sha(old) != p["old_sha"]:
        raise HTTPException(409, "Die Datei wurde inzwischen geändert (z. B. von Hermes selbst). Bitte neu laden.")
    bid = backup(key, old, p["desc"])
    if p["new"] is None:
        if path.exists():
            path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()
    else:
        mode = ENV_MODE if kind == "env" else None
        atomic_write(path, p["new"], mode)
        if p.get("mark_own"):
            with contextlib.suppress(OSError):
                (path.parent / ".nuorb-admin").write_text(now_iso() + "\n")
                if AFTER_WRITE:
                    AFTER_WRITE(path.parent / ".nuorb-admin")
    if p.get("after_ui"):
        try:
            write_ui_fields(p["after_ui"], "Gehirn-Modus (automatisch mitgeschrieben)")
        except Exception as exc:  # Änderung selbst ist gespeichert; nur das Mitschreiben fehlt, also melden
            print(f"[admin] Gehirn-Modus nicht in ui.json geschrieben: {exc.__class__.__name__}: {exc}", flush=True)
    if p.get("after_brain"):
        try:
            save_brain_saved(p["after_brain"])
        except Exception as exc:
            print(f"[admin] Gehirn-Einstellung nicht gesichert: {exc.__class__.__name__}: {exc}", flush=True)
    if (p.get("after_ui") or {}).get("brain_mode") == "remote":
        await unload_local_model()
    audit(who(request), f"geändert: {label_of(key)} · {p['desc']} (Sicherung {bid})")
    check = None
    if kind == "yaml" and key == "config":
        code, out = await run_hermes("config", "check", timeout=60)
        check = {"ok": code == 0, "output": out}
    restart = "" if p.get("after_ui") else restart_hint(key)  # Gehirn-Wechsel: Hermes liest config.yaml pro Runde neu (getestet)
    return {"ok": True, "backup": bid, "check": check, "restart": restart}


@app.delete("/api/change/{pid}")
async def change_discard(pid: str):
    PENDING.pop(pid, None)
    return {"ok": True}


@app.get("/api/backups")
async def backups_list():
    items = []
    for d in sorted(BACKUPS.iterdir(), reverse=True):
        with contextlib.suppress(OSError, ValueError, KeyError):   # unvollständige Sicherung überspringen
            m = json.loads((d / "meta.json").read_text())
            items.append({"id": d.name, "key": m["key"], "file": label_of(m["key"]), "time": m["time"],
                          "reason": m.get("reason", ""), "size": (d / "content").stat().st_size})
    return {"items": items[:300]}


@app.post("/api/backups/{bid}/restore")
async def backups_restore(bid: str):
    d = BACKUPS / bid
    if "/" in bid or ".." in bid or not d.is_dir():
        raise HTTPException(404, "Sicherung nicht gefunden.")
    m = json.loads((d / "meta.json").read_text())
    content = (d / "content").read_text(encoding="utf-8")
    new = content if m.get("existed", True) else None
    if new is None and not m["key"].startswith("skill:"):
        new = "{}\n" if m["key"] == "ui" else ""
    return make_preview(m["key"], new, f"Rückgängig: Stand vom {m['time'][:16].replace('T', ' ')} wiederherstellen")


# ------------------------------------------------------------------ Hermes-Konfiguration (YAML, kommentarerhaltend)
def load_config_rt() -> CommentedMap:
    text = read_text(FILES["config"][0])
    data = yaml_rt.load(text) if text.strip() else CommentedMap()
    return data if data is not None else CommentedMap()


def dump_rt(data) -> str:
    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    return buf.getvalue()


def to_plain(o):
    if isinstance(o, dict):
        return {str(k): to_plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_plain(x) for x in o]
    return o


def cfg_get(data, path: str, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def to_rt(v):
    if isinstance(v, dict):
        m = CommentedMap()
        for k, x in v.items():
            m[k] = to_rt(x)
        return m
    if isinstance(v, list):
        s = CommentedSeq([to_rt(x) for x in v])
        if all(not isinstance(x, (dict, list)) for x in v) and len(v) <= 8:
            s.fa.set_flow_style()
        return s
    return v


def cfg_set(data, path: str, value) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = CommentedMap()
        cur = cur[part]
    if value is None:
        cur.pop(parts[-1], None)
    else:
        new_key = parts[-1] not in cur
        last = list(cur.keys())[-1] if (new_key and isinstance(cur, CommentedMap) and len(cur)) else None
        cur[parts[-1]] = to_rt(value)
        if last is not None:
            move_trailing_comment(cur, last, parts[-1])


def move_trailing_comment(m: CommentedMap, old_key, new_key) -> None:
    """Kommentarblock hinter dem bisher letzten Schlüssel (z. B. Ueberschrift des nächsten Abschnitts)
    an den neuen letzten Schlüssel haengen, damit der neue Eintrag nicht hinter der Ueberschrift landet."""
    with contextlib.suppress(AttributeError, IndexError, KeyError, TypeError):   # nur Kosmetik der Kommentare
        item = m.ca.items.get(old_key)
        tok = item[2] if item and len(item) > 2 else None
        if tok is None or not tok.value:
            return
        val = tok.value
        if val.startswith("#"):  # Zeilenende-Kommentar bleibt beim alten Schlüssel
            nl = val.find("\n")
            if nl < 0:
                return
            keep, rest = val[:nl + 1], val[nl + 1:]
            if not rest.strip():
                return
            tok.value = keep
        else:
            rest = val
            item[2] = None
        from ruamel.yaml.tokens import CommentToken
        from ruamel.yaml.error import CommentMark
        m.ca.items[new_key] = [None, None, CommentToken(rest if rest.startswith("\n") else "\n" + rest, CommentMark(0), None), None]


def config_preview(changes: dict, desc: str, effect: str = "Wirkt nach Neustart des Hermes-Gateways.", danger: str = "",
                   allow_secret: tuple = ()):
    data = load_config_rt()
    for path, value in changes.items():
        if path not in allow_secret and SECRET_KEY_RE.search(path.split(".")[-1]) and path.split(".")[-1] not in ("unattended_mode",):
            raise HTTPException(400, "Schlüssel/Passwörter bitte unter ‚Schlüssel‘ setzen, nicht in der Konfiguration.")
        cfg_set(data, path, value)
    return make_preview("config", dump_rt(data), desc, effect, danger)


@app.get("/api/config")
async def config_get():
    data = to_plain(load_config_rt())
    return {"config": redact_tree(data)}


@app.post("/api/config/set")
async def config_set_ep(request: Request):
    """Expertenmodus: einzelner Pfad mit YAML-Wert."""
    d = await body(request)
    path = str(d.get("path", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*", path) or path.startswith("_"):
        raise HTTPException(400, "Ungültiger Pfad (Beispiel: agent.max_turns).")
    raw = str(d.get("value", ""))
    if d.get("remove"):
        value = None
    else:
        try:
            value = YAML(typ="safe").load(raw) if raw.strip() else ""
        except YAMLError as e:
            raise HTTPException(400, f"Wert ist kein gültiges YAML: {e}")
    return config_preview({path: value}, f"{path} {'entfernen' if value is None else 'setzen'}")


@app.post("/api/config/multi")
async def config_multi(request: Request):
    """Mehrere Pfade auf einmal (eine Vorschau). Werte kommen als JSON, nicht als YAML."""
    d = await body(request)
    ch = d.get("changes")
    if not isinstance(ch, dict) or not ch:
        raise HTTPException(400, "Nichts zu speichern.")
    for path in ch:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*", path) or path.startswith("_"):
            raise HTTPException(400, f"Ungültiger Pfad: {path}")
    return config_preview(ch, str(d.get("desc") or "Einstellungen ändern")[:120])


# ------------------------------------------------------------------ Status / Uebersicht
CA_CTX = None


def ui_ssl_ctx():
    global CA_CTX
    if CA_CTX is None:
        CA_CTX = ssl.create_default_context(cafile=str(UI_CERTS / "nuorb-ca.crt"))
    return CA_CTX


async def probe(url: str, timeout: float = 1.5, headers: dict | None = None, verify=True):
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
            r = await c.get(url, headers=headers)
            try:
                return r.status_code < 400, r.json()
            except ValueError:
                return r.status_code < 400, None
    except (httpx.HTTPError, OSError):
        return False, None


async def unit_state(name: str) -> dict:
    unit = UNITS[name]
    p = await asyncio.create_subprocess_exec(
        "systemctl", "show", unit, "-p", "ActiveState,SubState,ActiveEnterTimestamp,MemoryCurrent,NRestarts,MainPID",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    kv = dict(l.split("=", 1) for l in out.decode().splitlines() if "=" in l)
    mem = kv.get("MemoryCurrent", "")
    return {"id": name, "unit": unit, "label": UNIT_LABELS[name], "active": kv.get("ActiveState"),
            "sub": kv.get("SubState"), "since": kv.get("ActiveEnterTimestamp"),
            "memory": int(mem) if mem.isdigit() else None, "restarts": kv.get("NRestarts")}


def sysinfo() -> dict:
    with open("/proc/meminfo") as f:
        mi = {l.split(":")[0]: int(l.split()[1]) for l in f}
    du = shutil.disk_usage(str(HOME))
    with open("/proc/loadavg") as f:
        load = f.read().split()[:3]
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    return {"mem_total": mi["MemTotal"] * 1024, "mem_avail": mi["MemAvailable"] * 1024, "disk_total": du.total,
            "disk_free": du.free, "load": load, "uptime": up, "cpus": os.cpu_count()}


# ------------------------------------------------------------------ Gerät koppeln (NuOrb-App)
UI_PUBLIC_URL = os.environ.get("UI_PUBLIC_URL", "")  # leer = Host des Browser-Aufrufs, Port UI_PUBLIC_PORT
UI_PUBLIC_PORT = os.environ.get("UI_PUBLIC_PORT", "8443")


def ca_fingerprint() -> str:
    """SHA-256 ueber das oeffentliche Heim-CA-Zertifikat (DER), 64 Hex gross. Leer, wenn keins da ist."""
    try:
        der = ssl.PEM_cert_to_DER_cert((UI_CERTS / "nuorb-ca.crt").read_text())
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(der).hexdigest().upper()


def pair_ui_url(request: Request) -> str:
    if UI_PUBLIC_URL:
        return UI_PUBLIC_URL.rstrip("/")
    host = request.url.hostname or "localhost"
    host = f"[{host}]" if ":" in host else host
    return f"https://{host}:{UI_PUBLIC_PORT}"


# Mehrere Smartphones: Einmal-Codes + Geraeteliste, Dateiformat wie im Docker-Image (nuorb_common):
# pairing.json {sha256(code): {exp, by, created}}, devices.json {id: {name, platform, token: sha256, created, last_seen, ip}}.
# Eingeloest wird der Code von nuorb-ui (POST /api/pair). Kann nuorb-ui das (noch) nicht, gibt es den QR ohne Code.
PAIR_DIR = UI_SETTINGS.parent
PAIRING_FILE, DEVICES_FILE = PAIR_DIR / "pairing.json", PAIR_DIR / "devices.json"
CODE_TTL = 600
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O/1/I
PAIR_PROBE: dict = {"t": 0.0, "ok": False}


@contextlib.contextmanager
def json_lock(p: Path):
    """Sperre über Prozessgrenzen (Oberfläche und Verwaltung schreiben dieselben Dateien): Lesen, Ändern und
    Schreiben von devices.json und pairing.json als ein Schritt. Sperrdatei <datei>.lock daneben, 0600."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _json_read(p: Path, default):
    with contextlib.suppress(FileNotFoundError, ValueError):
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def _json_write(p: Path, data) -> None:
    atomic_write(p, json.dumps(data, indent=1, ensure_ascii=False), 0o600)


async def ui_pairing_supported() -> bool:
    """Kann nuorb-ui Kopplungscodes einloesen? (GET /api/info gibt es nur mit Kopplung.) 60 s gemerkt."""
    if time.time() - PAIR_PROBE["t"] < 60:
        return PAIR_PROBE["ok"]
    ok, j = await probe(f"{UI_URL}/api/info", 2, verify=ui_ssl_ctx())
    ok = bool(ok and isinstance(j, dict) and "pairing_required" in j)
    PAIR_PROBE.update(t=time.time(), ok=ok)
    return ok


def qr_data(text: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def pair_payload(request: Request, code: str | None) -> dict:
    url, fp = pair_ui_url(request), ca_fingerprint()
    name = os.environ.get("NUORB_NAME", "")[:40] or ui_brand()["brand_name"]
    q = {"v": "1", "url": url, **({"code": code} if code else {}), **({"fp": fp} if fp else {}), "name": name}
    uri = "nuorb://pair?" + urllib.parse.urlencode(q)
    return {"uri": uri, "url": url, "fingerprint": fp, "name": name, "code": code, "qr": qr_data(uri)}


@app.get("/api/pairing/link")
async def pairing_link(request: Request):
    """QR ohne Code (gilt fuer beliebig viele Geraete, solange nuorb-ui keine Kopplung verlangt)."""
    return {**pair_payload(request, None), "pairing_supported": await ui_pairing_supported()}


@app.post("/api/pairing/new")
async def pairing_new(request: Request):
    if not await ui_pairing_supported():
        return {**pair_payload(request, None), "pairing_supported": False}
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    async with BRAIN_LOCK:
        with json_lock(PAIRING_FILE):
            data = {k: v for k, v in _json_read(PAIRING_FILE, {}).items() if v.get("exp", 0) > time.time()}
            exp = time.time() + CODE_TTL
            data[hashlib.sha256(raw.encode()).hexdigest()] = {"exp": exp, "by": who(request)[:40], "created": now_iso()}
            _json_write(PAIRING_FILE, data)
    audit(who(request), "Kopplungscode erzeugt")
    code = f"{raw[:4]}-{raw[4:]}"
    return {**pair_payload(request, code), "expires": int(exp), "ttl": CODE_TTL, "pairing_supported": True}


def _age_s(iso: str | None) -> float | None:
    try:
        return time.time() - datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def phones_summary() -> dict:
    """Gekoppelte NuOrb-Apps: Anzahl, davon aktiv (last_seen juenger als PHONE_ONLINE_S), zuletzt gesehenes Geraet."""
    devs = sorted(_json_read(DEVICES_FILE, {}).values(), key=lambda v: v.get("last_seen") or "", reverse=True)
    online = sum(1 for v in devs if (a := _age_s(v.get("last_seen"))) is not None and a < PHONE_ONLINE_S)
    last = {"name": devs[0].get("name"), "last_seen": devs[0].get("last_seen")} if devs else None
    return {"paired": len(devs), "online": online, "last": last, "online_s": PHONE_ONLINE_S}


@app.get("/api/pairing/devices")
async def pairing_devices():
    out = [{"id": k, **{x: v.get(x) for x in ("name", "platform", "created", "last_seen", "ip")}}
           for k, v in _json_read(DEVICES_FILE, {}).items()]
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return {"devices": out, "pairing_supported": await ui_pairing_supported(), "fingerprint": ca_fingerprint()}


@app.post("/api/pairing/revoke")
async def pairing_revoke(request: Request):
    d = await body(request)
    did = str(d.get("id", ""))
    async with BRAIN_LOCK:
        with json_lock(DEVICES_FILE):
            devs = _json_read(DEVICES_FILE, {})
            if did not in devs:
                raise HTTPException(404, "Gerät nicht gefunden.")
            name = devs.pop(did).get("name", did)
            _json_write(DEVICES_FILE, devs)
    audit(who(request), f"Gerät entkoppelt: {name}")
    return {"ok": True}


LOCAL_PROVIDER = OLLAMA_PROVIDER  # Vertrag §1.3: Eintrag heisst weiter pc-ollama, auch fuer llama.cpp & Co.
LLM_TYPES = ("ollama", "openai")
LLM_LOOPBACK = "localhost zeigt auf den NuOrb-Server selbst, nicht auf deinen PC. Trage die IP-Adresse des PCs ein."
LLM_PORT_HINT = "Port fehlt? llama.cpp 8080, vLLM 8000, LM Studio 1234, LocalAI 8080."


def llm_normalize(raw: str, typ: str = "ollama") -> tuple[str, str]:
    """Nutzereingabe -> Basis-URL ohne /v1 plus Warnhinweis. ollama: Port 11434 ergaenzen, kein Pfad.
    openai: Pfad (Reverse-Proxy) erlaubt, Port wird nicht geraten."""
    s = raw.strip().rstrip("/")
    if not s:
        raise HTTPException(400, "Bitte eine Ollama-Adresse eingeben, z. B. http://192.168.1.50:11434." if typ == "ollama"
                            else "Bitte eine Server-Adresse eingeben, z. B. http://192.168.1.50:8080.")
    if "://" not in s:
        s = "http://" + s
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(400, "Die Server-Adresse muss mit http:// oder https:// beginnen.")
    if u.query or u.fragment or u.username or u.password:
        raise HTTPException(400, "Die Server-Adresse darf keine Zugangsdaten, Parameter oder Anker enthalten. Ein API-Key gehört ins eigene Feld.")
    path = re.sub(r"/(v1/chat/completions|v1/models|v1|api)$", "", u.path.rstrip("/"))
    if path and typ == "ollama":
        raise HTTPException(400, "Bitte nur Adresse und Port angeben, ohne Pfad (z. B. http://192.168.1.50:11434).")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    has_port = u.port is not None
    port = u.port or (11434 if typ == "ollama" and u.scheme == "http" else None)
    url = f"{u.scheme}://{host}" + (f":{port}" if port else "") + path
    warn = []
    loop = u.hostname in ("localhost", "0.0.0.0")
    with contextlib.suppress(ValueError):
        loop = loop or ipaddress.ip_address(u.hostname).is_loopback
    if loop:
        warn.append(LLM_LOOPBACK)
    if typ == "openai" and not has_port and u.scheme == "http":
        warn.append(LLM_PORT_HINT)
    return url, " ".join(warn)


def ollama_normalize(raw: str) -> tuple[str, str]:
    """Kompatibilitaet (Docker-Huelle): wie llm_normalize(raw, "ollama")."""
    return llm_normalize(raw, "ollama")


def llm_type() -> str:
    """Typ des lokalen Gehirns aus brain_saved.local.type; fehlt = ollama (alte Installationen bleiben gleich)."""
    t = (load_brain_saved().get("local") or {}).get("type")
    return t if t in LLM_TYPES else "ollama"


def llm_provider(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else to_plain(load_config_rt())
    return ((cfg.get("providers") or {}).get(LOCAL_PROVIDER) or {})


def llm_key(cfg: dict | None = None) -> str:
    return str(llm_provider(cfg).get("api_key") or "").strip()


def llm_server(cfg: dict | None = None, typ: str | None = None) -> str:
    typ = typ or llm_type()
    name = str(llm_provider(cfg).get("name") or "").strip()
    return "Ollama" if typ == "ollama" else (name if name and not name.startswith("PC Ollama") and name != "Ollama" else "OpenAI-kompatibel")


def llm_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


CTX_MIN = 65536


def ctx_short_text(name: str, ctx: int) -> str:
    return (f"Modell {name} läuft nur mit {ctx} Token Kontext. Lange Gespräche können abgeschnitten werden. "
            f"Ollama begrenzt auf das Trainingsfenster des Modells; ein größeres Modell wählen.")


def own_ip_towards(host: str, port: int, prefer: str = "") -> str:
    """IP, unter der der LLM-Rechner diesen NuOrb-Server sieht: NUORB_HOST_IP, sonst die Browser-Adresse
    (wenn IPv4), sonst die Quelladresse der Route zum Ziel."""
    env_ip = os.environ.get("NUORB_HOST_IP", "").strip()
    if env_ip:
        return env_ip
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(prefer)
        if ip.version == 4 and not ip.is_loopback:
            return prefer
    with contextlib.suppress(OSError):
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s_.connect((host, port or 80))
            return s_.getsockname()[0]
        finally:
            s_.close()
    return "<NuOrb-IP>"


def firewall_hint(base: str, prefer_ip: str = "") -> dict:
    u = urllib.parse.urlsplit(base)
    port = u.port or (443 if u.scheme == "https" else 80)
    ip = own_ip_towards(u.hostname or "", port, prefer_ip)
    return {"ip": ip, "port": port,
            "ufw": f"sudo ufw allow from {ip} to any port {port} proto tcp",
            "firewalld": (f"sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address={ip} "
                          f"port port={port} protocol=tcp accept' && sudo firewall-cmd --reload"),
            "windows": (f'New-NetFirewallRule -DisplayName "NuOrb LLM" -Direction Inbound -Protocol TCP '
                        f'-LocalPort {port} -RemoteAddress {ip} -Action Allow')}


async def llm_detect(typ: str, base: str, key: str = "", prefer_ip: str = "") -> dict:
    """Verbindungstest + Server-Erkennung (Vertrag §1.4; Referenz scripts/llm-backend-test.py detect()).
    Erkennung nie am Feld `models` (liefert llama.cpp auch). Aendert nichts."""
    out = {"ok": False, "base": base, "type": typ, "server": "Ollama" if typ == "ollama" else "OpenAI-kompatibel",
           "version": None, "models": [], "sizes": {}, "ctx": None, "warn": "", "error": ""}
    async with httpx.AsyncClient(timeout=5, headers=llm_headers(key)) as c:
        async def get(path: str, t: float = 5):
            try:
                r = await c.get(base + path, timeout=t)
            except httpx.TimeoutException:
                return "timeout", None
            except httpx.HTTPError:
                return "conn", None
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        st, m = await get("/v1/models")
        if st in ("timeout", "conn"):
            out["firewall"] = firewall_hint(base, prefer_ip)
        if st == "timeout":
            out["error"] = f"{base}: nicht erreichbar (Firewall? Server nur auf 127.0.0.1?)"
            return out
        if st == "conn":
            out["error"] = (f"Keine Verbindung zu {base}. Adresse und Port prüfen; Ollama muss im Netz lauschen (OLLAMA_HOST=0.0.0.0)."
                            if typ == "ollama" else f"Server nicht erreichbar: {base}. Adresse und Port prüfen.")
            return out
        if st in (401, 403):
            out["error"] = "Der API-Key wird abgelehnt." if key else "Der Server verlangt einen API-Key."
            return out
        if st != 200 or not isinstance(m, dict) or not isinstance(m.get("data"), list):
            out["error"] = ("Unter dieser Adresse läuft kein LLM-Server (keine gültige Antwort von /v1/models)." if st == 200
                            else "Das ist kein OpenAI-kompatibler Server (kein /v1/models)." if st == 404 else f"/v1/models antwortet mit {st}.")
            return out
        data = [d for d in (m.get("data") or []) if isinstance(d, dict)]
        out["models"] = [d.get("id") for d in data if d.get("id")]
        owned = {d.get("owned_by") for d in data}
        st, v = await get("/api/version", 3)
        tags = None
        if st == 200 and isinstance(v, dict) and "version" in v:  # nur mit /api/tags (JSON mit models) – fremde Web-Apps antworten auf alles
            st2, tags = await get("/api/tags", 4)
            if not (st2 == 200 and isinstance(tags, dict) and isinstance(tags.get("models"), list)):
                tags = None
        is_ollama = tags is not None or "library" in owned
        if typ == "ollama":
            if not is_ollama:
                out["error"] = f"{base} antwortet, ist aber kein Ollama. Server-Typ „OpenAI-kompatibel“ wählen."
                return out
            out["version"] = (v or {}).get("version") if isinstance(v, dict) else None
            st3, psj = await get("/api/ps", 3)
            if st3 == 200 and isinstance(psj, dict):
                short = [(p.get("name"), p.get("context_length")) for p in psj.get("models") or []
                         if isinstance(p.get("context_length"), int) and p.get("context_length") < CTX_MIN]
                if short:
                    out["ctx_short"] = [{"name": n, "ctx": c} for n, c in short]
                    out["warn"] = " ".join(ctx_short_text(n, c) for n, c in short)
            if tags is not None:
                tl = tags.get("models") or []
                out["models"] = sorted(t.get("name") for t in tl if t.get("name")) or out["models"]
                out["sizes"] = {t.get("name"): t.get("size") for t in tl}
        elif is_ollama:
            out["server"], out["version"] = "Ollama", (v or {}).get("version") if isinstance(v, dict) else None
        else:
            st, p = await get("/props", 3)
            if "llamacpp" in owned or (st == 200 and isinstance(p, dict) and "default_generation_settings" in p):
                out["server"] = "llama.cpp"
                if isinstance(p, dict):
                    out["version"] = p.get("build_info")
                    out["ctx"] = (p.get("default_generation_settings") or {}).get("n_ctx")
            else:
                st, v = await get("/version", 3)
                if "vllm" in owned or (st == 200 and isinstance(v, dict) and "version" in v):
                    out["server"] = "vLLM"
                    out["version"] = v.get("version") if isinstance(v, dict) else None
                    out["ctx"] = next((d.get("max_model_len") for d in data if d.get("max_model_len")), None)
                else:
                    st, v = await get("/api/v0/models", 3)
                    if (st == 200 and isinstance(v, dict) and isinstance(v.get("data"), list)) or "organization_owner" in owned:
                        out["server"] = "LM Studio"
                        lm = lmstudio_info(v)
                        if lm:
                            out["models"] = lm["models"] or out["models"]
                            out["ctx"] = lm["ctx"]
                            if lm["warn"]:
                                out["warn"] = lm["warn"]
                    else:
                        st, _ = await get("/readyz", 3)
                        if st == 200:
                            out["server"] = "LocalAI"
    if isinstance(out["ctx"], int) and out["ctx"] < CTX_MIN:
        out["warn"] = f"Der Server bietet nur {out['ctx']} Token Kontext, NuOrb braucht mindestens 64k. Startbefehl siehe Hilfe."
    out["ok"] = True
    return out


def lmstudio_info(v) -> dict | None:
    """LM Studio /api/v0/models: nur Sprachmodelle (keine Embedding-Modelle) und der Kontext der geladenen Modelle.
    LM Studio lädt ein nicht geladenes Modell bei der ersten Anfrage mit seinem Standard-Kontext (oft 4096) –
    dann scheitert schon die erste Frage. Deshalb warnen, wenn kein Sprachmodell mit Kontext geladen ist."""
    if not isinstance(v, dict) or not isinstance(v.get("data"), list):
        return None
    llms = [d for d in v["data"] if isinstance(d, dict) and d.get("id") and d.get("type", "llm") in ("llm", "vlm")]
    loaded = [d for d in llms if d.get("state") == "loaded" and isinstance(d.get("loaded_context_length"), int)]
    ctx = min((d["loaded_context_length"] for d in loaded), default=None)
    warn = "" if loaded else ("In LM Studio ist kein Sprachmodell geladen. Modell mit 64k Kontext laden: "
                              "lms load <modell> -c 65536 (oder Context Length 65536 beim Laden in der App).")
    # geladene zuerst, damit die Auswahl auf das laufende Modell fällt
    return {"models": [d["id"] for d in loaded] + [d["id"] for d in llms if d not in loaded], "ctx": ctx, "warn": warn}


async def llm_tools_probe(base: str, key: str, model: str) -> dict:
    """Vertrag §1.4 Schritt 5: kann der Server Tool-Aufrufe? (eine kleine Anfrage, bis 120 s)"""
    body_ = {"model": model, "messages": [{"role": "user", "content": "Wie spät ist es? Nutze das Werkzeug."}],
             "tools": [{"type": "function", "function": {"name": "get_time", "description": "Aktuelle Uhrzeit",
                                                          "parameters": {"type": "object", "properties": {}}}}],
             "tool_choice": "auto", "max_tokens": 400}
    try:
        async with httpx.AsyncClient(timeout=120, headers=llm_headers(key)) as c:
            r = await c.post(f"{base}/v1/chat/completions", json=body_)
    except httpx.HTTPError:
        return {"ok": False, "error": "Keine Antwort vom Server."}
    try:
        j = r.json()
    except ValueError:
        j = None
    calls = ((((j or {}).get("choices") or [{}])[0].get("message") or {}).get("tool_calls")) if isinstance(j, dict) else None
    if r.status_code == 200 and calls:
        return {"ok": True, "error": ""}
    return {"ok": False, "status": r.status_code, "error": "Der Server hat kein Werkzeug aufgerufen."}


def ollama_base(cfg: dict | None = None) -> str:
    """Die eine Ollama-Adresse (ohne /v1): providers.pc-ollama.api in config.yaml, sonst model.base_url
    (wenn das Gehirn lokal ist), sonst OLLAMA_URL aus der Umgebung, sonst leer."""
    cfg = cfg if cfg is not None else to_plain(load_config_rt())
    cand = [((cfg.get("providers") or {}).get(OLLAMA_PROVIDER) or {}).get("api")]
    model = cfg.get("model") or {}
    if str(model.get("provider", "")).startswith("custom"):
        cand.append(model.get("base_url"))
    cand.append(OLLAMA_ENV)
    for c in cand:
        c = str(c or "").strip().rstrip("/")
        if c:
            return re.sub(r"/v1$", "", c)
    return ""


@app.get("/api/status")
async def status():
    cfg = to_plain(load_config_rt())
    model = cfg.get("model", {}) or {}
    ollama, typ = ollama_base(cfg), llm_type()
    async def nothing():
        return False, None
    if not ollama:
        brain_p, ps_p = nothing(), nothing()
    elif typ == "openai":  # Vertrag §1.6: kein /api/ps, /api/version – online = /v1/models mit Key
        brain_p, ps_p = probe(f"{ollama}/v1/models", 3, headers=llm_headers(llm_key(cfg))), nothing()
    else:
        brain_p, ps_p = probe(f"{ollama}/api/version", 3), probe(f"{ollama}/api/ps", 3)
    (brain, ver), (ps_ok, ps), (hermes, _), (stt, sttj), (ui, uij) = await asyncio.gather(
        brain_p, ps_p, probe(f"{HERMES_URL}/health"),
        probe(STT_URL), probe(f"{UI_URL}/api/status", verify=ui_ssl_ctx()))
    units = await asyncio.gather(*(unit_state(n) for n in UNITS))
    loaded = []
    if ps_ok and ps:
        for m in ps.get("models", []):
            loaded.append({"name": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"),
                           "until": m.get("expires_at"), "ctx": m.get("context_length")})
    fb = cfg.get("fallback_providers") or []
    return {
        "time": now_iso(), "brain": {"online": brain, "version": (ver or {}).get("version") if typ == "ollama" else None,
                                     "url": ollama, "loaded": loaded, "type": typ, "server": llm_server(cfg, typ)},
        "hermes": {"online": hermes}, "stt": {"online": stt, "info": sttj}, "ui": {"online": ui, "info": uij},
        "model": {"default": model.get("default"), "provider": model.get("provider"),
                  "context": model.get("context_length"), "num_ctx": model.get("ollama_num_ctx")},
        "fallback": [{"provider": f.get("provider"), "model": f.get("model")} for f in fb if isinstance(f, dict)],
        "approvals": (cfg.get("approvals") or {}).get("mode"),
        "mode": brain_state()["mode"],
        "units": units, "system": sysinfo(), "phone": await phone_status(cached=True) if PHONE_HOST and not KIOSK_PAUSED else None,
        "kiosk": {"configured": bool(PHONE_HOST), "paused": KIOSK_PAUSED}, "phones": phones_summary(),
        "gpu_label": GPU_LABEL or None,
        "restart": ctl_result(),
    }


# ------------------------------------------------------------------ Modell & Fallback
@app.get("/api/models")
async def models():
    cfg = to_plain(load_config_rt())
    model = cfg.get("model", {}) or {}
    ollama, typ = ollama_base(cfg), llm_type()
    if typ == "openai":
        ok, mods = (await probe(f"{ollama}/v1/models", timeout=3, headers=llm_headers(llm_key(cfg)))) if ollama else (False, None)
        names = sorted(d.get("id") for d in (mods or {}).get("data", []) if isinstance(d, dict) and d.get("id")) if ok else []
        sizes = {}
    else:
        ok, tags = (await probe(f"{ollama}/api/tags", timeout=3)) if ollama else (False, None)
        names = sorted(m.get("name") for m in (tags or {}).get("models", [])) if ok else []
        sizes = {m.get("name"): m.get("size") for m in (tags or {}).get("models", [])} if ok else {}
    providers = cfg.get("providers") or {}
    return {"ollama": names, "sizes": sizes, "online": ok, "model": model, "ollama_url": ollama,
            "llm_type": typ, "llm_server": llm_server(cfg, typ), "api_key_set": bool(llm_key(cfg)),
            "reasoning": (cfg.get("agent") or {}).get("reasoning_effort"),
            "fallback": cfg.get("fallback_providers") or [],
            "providers": list(providers.keys()),
            "anthropic_key": mask(env_value("ANTHROPIC_API_KEY")),
            "custom_models": {k: list(((v or {}).get("models") or {}).keys()) for k, v in providers.items()}}


@app.post("/api/models/save")
async def models_save(request: Request):
    d = await body(request)
    ch = {}
    if "default" in d:
        name = str(d["default"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,120}", name):
            raise HTTPException(400, "Ungültiger Modellname.")
        ch["model.default"] = name
    cfg = to_plain(load_config_rt())
    old_typ = llm_type()
    typ = str(d.get("llm_type") or old_typ)
    if typ not in LLM_TYPES:
        raise HTTPException(400, "Unbekannter Server-Typ.")
    raw = d.get("url") or d.get("ollama_url")
    base = ollama_base(cfg)
    if raw:
        url, _ = llm_normalize(str(raw), typ)
        if url != base:
            ch[f"providers.{LOCAL_PROVIDER}.api"] = url + "/v1"
            if str((cfg.get("model") or {}).get("provider", "")) == f"custom:{LOCAL_PROVIDER}":
                ch["model.base_url"] = url + "/v1"
        base = url
    key = llm_key(cfg)
    new_key = str(d.get("api_key") or "").strip()
    if new_key:
        if "\n" in new_key or len(new_key) > 4096:
            raise HTTPException(400, "Ungültiger API-Key.")
        if new_key != key:
            ch[f"providers.{LOCAL_PROVIDER}.api_key"] = new_key
        key = new_key
    elif d.get("api_key_clear") and key:
        ch[f"providers.{LOCAL_PROVIDER}.api_key"] = None
        key = ""
    det = await llm_detect(typ, base, key) if base and (raw or typ != old_typ or new_key) else None
    server = det["server"] if det and det["ok"] else llm_server(cfg, typ)
    old_name = str(llm_provider(cfg).get("name") or "")
    if typ == "ollama":  # eigenen Anzeigenamen (z. B. „PC Ollama (…)“) behalten; nur nach Typwechsel oder wenn leer setzen
        server = "Ollama"
        if not old_name or old_typ != "ollama":
            ch[f"providers.{LOCAL_PROVIDER}.name"] = server
    elif server != old_name and (det or not old_name):
        ch[f"providers.{LOCAL_PROVIDER}.name"] = server
    if "context_length" in d:
        n = int(d["context_length"]) if d["context_length"] else None
        if n is not None and not 2048 <= n <= 1048576:
            raise HTTPException(400, "Kontext zwischen 2048 und 1048576.")
        ch["model.context_length"] = n
        ch["model.ollama_num_ctx"] = n if typ == "ollama" else None  # Vertrag §1.3: bei openai nicht setzen
    elif typ == "openai" and (cfg.get("model") or {}).get("ollama_num_ctx") is not None:
        ch["model.ollama_num_ctx"] = None
    if "reasoning" in d:
        r = str(d["reasoning"])
        if r not in ("none", "minimal", "low", "medium", "high", "xhigh"):
            raise HTTPException(400, "Ungültige Denktiefe.")
        ch["agent.reasoning_effort"] = r
    if "fallback" in d:
        fb = []
        for f in d["fallback"] or []:
            prov, mod = str(f.get("provider", "")).strip(), str(f.get("model", "")).strip()
            if not prov or not mod or not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,120}", prov + mod):
                raise HTTPException(400, "Jeder Fallback braucht Anbieter und Modell.")
            fb.append({"provider": prov, "model": mod})
        ch["fallback_providers"] = fb
    if not ch:
        raise HTTPException(400, "Nichts zu speichern.")
    danger = ""
    if "model.default" in ch:
        danger = "Ein anderes Modell kann mehr Grafikspeicher (VRAM) auf dem PC belegen. Vorher prüfen, ob es mit 64k Kontext passt."
    pv = config_preview(ch, "Modell/Fallback ändern", danger=danger, allow_secret=(f"providers.{LOCAL_PROVIDER}.api_key",))
    saved = load_brain_saved()
    loc = dict(saved.get("local") or {})
    if loc.get("type", "ollama") != typ or loc.get("server") != server:
        loc.update(type=typ, server=server)
        PENDING[pv["preview"]]["after_brain"] = {**saved, "local": loc}
    return pv


@app.post("/api/llm/test")
async def llm_test(request: Request):
    """Vertrag §1.4: {type, url, api_key} -> {ok, base, server, version, models, ctx, warn, error}. Aendert nichts.
    Leerer api_key = gespeicherten Key nehmen (nie zurueckgeben)."""
    d = await body(request)
    typ = str(d.get("type") or "ollama")
    if typ not in LLM_TYPES:
        raise HTTPException(400, "Unbekannter Server-Typ.")
    base, warn = llm_normalize(str(d.get("url", "")), typ)
    key = str(d.get("api_key") or "").strip() or ("" if d.get("api_key_clear") else llm_key())
    r = await llm_detect(typ, base, key, request.url.hostname or "")
    r["warn"] = " ".join(x for x in (warn, r["warn"]) if x)
    return r


@app.post("/api/llm/tools")
async def llm_tools(request: Request):
    d = await body(request)
    typ = str(d.get("type") or "ollama")
    base, _ = llm_normalize(str(d.get("url", "")), typ if typ in LLM_TYPES else "ollama")
    model = str(d.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "Bitte zuerst ein Modell wählen.")
    key = str(d.get("api_key") or "").strip() or llm_key()
    return await llm_tools_probe(base, key, model)


@app.post("/api/ollama/test")
async def ollama_test(request: Request):
    """Kompatibilitaet: Ollama-Test im alten Format (Docker-Einrichtung)."""
    d = await body(request)
    url, warn = llm_normalize(str(d.get("url", "")), "ollama")
    r = await llm_detect("ollama", url)
    if not r["ok"]:
        raise HTTPException(502, r["error"])
    return {"ok": True, "url": url, "version": r["version"], "count": len(r["models"]), "models": r["models"],
            "sizes": r["sizes"], "warn": warn}


@app.post("/api/models/check")
async def models_check(request: Request):
    d = await body(request)
    live = bool(d.get("live"))
    if live:
        audit(who(request), "Live-Prüfung Claude (< 0,1 Cent)")
    code, out = await run_hermes("fallback", "check", *(["--live"] if live else []), timeout=120)
    return {"ok": code == 0, "output": out}


@app.post("/api/models/unload")
async def models_unload(request: Request):
    d = await body(request)
    name = str(d.get("name", ""))
    cfg = to_plain(load_config_rt())
    ollama = ollama_base(cfg)
    if not ollama:
        raise HTTPException(400, "Keine Ollama-Adresse eingestellt.")
    if llm_type() != "ollama":
        raise HTTPException(400, "Entladen gibt es nur bei Ollama.")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{ollama}/api/generate", json={"model": name, "keep_alive": 0})
    audit(who(request), f"Modell entladen: {name}")
    return {"ok": r.status_code < 400}


# ------------------------------------------------------------------ Gehirn-Modus (Lokal / Automatisch / Remote)
# Fallback und Projekt-Claude stehen als markierte Bloecke in config.yaml (Format der Sitzung sprachsteuerung,
# scripts/steuerung-fallback-setzen.py). Wir schreiben exakt dasselbe Format, aber ueber Vorschau/Sicherung/Rueckgaengig.
CLAUDE_MODELS = {
    "claude-haiku-5-5": {"name": "Claude Haiku 5.5", "in": 0.10, "out": 0.50, "hint": "schnell, sehr günstig"},
    "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "in": 2.0, "out": 10.0, "hint": "ausgewogen"},
    "claude-opus-5-5": {"name": "Claude Opus 5.5", "in": 4.0, "out": 20.0, "hint": "am stärksten, denkt immer"},
}
LOCAL_DEFAULT = {"provider": f"custom:{OLLAMA_PROVIDER}", "base_url": "", "default": ""}  # Adresse: ollama_base()
FB_BLOCK = re.compile(r"# >>> nuorb-claude-fallback.*?# <<< nuorb-claude-fallback\n", re.S)
PC_BLOCK = re.compile(r"^(?:  )?# >>> nuorb-projekt-claude[^\n]*\n.*?^(?:  )?# <<< nuorb-projekt-claude[^\n]*\n", re.S | re.M)  # neues Format: eingerueckt in mcp_servers
BRAIN_FILE = DATA / "brain.json"
MODE_LABEL = {"local": "Lokal", "auto": "Automatisch", "remote": "Remote"}


def fb_block_model(text: str) -> str | None:
    m = FB_BLOCK.search(text)
    mm = re.search(r"model:\s*(\S+)", m.group(0)) if m else None
    return mm.group(1) if mm else None


def fb_block_set(text: str, model: str | None) -> str:
    ohne = FB_BLOCK.sub("", text)
    if re.search(r"^fallback_providers:", ohne, re.M):
        raise HTTPException(409, "fallback_providers steht ausserhalb des markierten Blocks. Bitte im Expertenmodus bereinigen.")
    neu = ohne.rstrip("\n") + "\n"
    if model:
        neu += ("# >>> nuorb-claude-fallback (scripts/steuerung-claude-fallback.sh)\n"
                f"fallback_providers:\n  - provider: anthropic\n    model: {model}\n# <<< nuorb-claude-fallback\n")
    return neu


def load_brain_saved() -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(BRAIN_FILE.read_text())
    return {}


def save_brain_saved(d: dict) -> None:
    atomic_write(BRAIN_FILE, json.dumps(d), 0o600)


def brain_state() -> dict:
    """Ist-Zustand. Remote- und Fallback-Modell werden getrennt gefuehrt (data/brain.json merkt das jeweils
    nicht aktive), damit z. B. Remote = Sonnet und Fallback = Haiku unabhaengig waehlbar sind."""
    text = read_text(FILES["config"][0])
    cfg = to_plain(yaml_rt.load(text) or {})
    model = cfg.get("model") or {}
    fb = fb_block_model(text)
    remote = str(model.get("provider", "")).startswith("anthropic")
    mode = "remote" if remote else ("auto" if (fb or cfg.get("fallback_providers")) else "local")
    pc = PC_BLOCK.search(text)
    pm = re.search(r"CLAUDE_MODELL:\s*(\S+)", pc.group(0)) if pc else None
    saved = load_brain_saved()
    local = {**LOCAL_DEFAULT, **{k: v for k, v in (saved.get("local") or {}).items() if v}}
    remote_model = model.get("default") if remote else saved.get("remote_model", "claude-sonnet-5-5")
    fallback_model = fb or saved.get("fallback_model", "claude-haiku-5-5")
    active = remote_model if remote else (model.get("default") or local["default"])
    return {"mode": mode, "label": MODE_LABEL[mode], "active_model": active,
            "remote_model": remote_model, "fallback_model": fallback_model, "fallback": fb,
            "claude": remote_model if remote else fb,  # Kompatibilitaet
            "local_model": local["default"] if remote else model.get("default"),
            "projekt": pm.group(1) if pm else None, "models": CLAUDE_MODELS,
            "anthropic_key": mask(env_value("ANTHROPIC_API_KEY"))}


def build_brain_change(mode: str, remote_model: str | None, fallback_model: str | None) -> dict:
    """Gemeinsame Logik fuer Admin-UI und Smartphone: liefert neuen config.yaml-Text + Texte + ui.json-Felder."""
    if mode not in MODE_LABEL:
        raise HTTPException(400, "Ungültiger Modus (local, auto oder remote).")
    st = brain_state()
    remote_model = remote_model or st["remote_model"]
    fallback_model = fallback_model or st["fallback_model"]
    if remote_model not in CLAUDE_MODELS or fallback_model not in CLAUDE_MODELS:
        raise HTTPException(400, "Unbekanntes Claude-Modell (Haiku, Sonnet oder Opus 5.5).")
    if mode != "local" and not env_value("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "Für Claude fehlt der ANTHROPIC_API_KEY (in der Verwaltung unter Schlüssel eintragen).")
    text = read_text(FILES["config"][0])
    cur = to_plain(yaml_rt.load(text) or {}).get("model") or {}
    saved = load_brain_saved()
    if not str(cur.get("provider", "")).startswith("anthropic"):  # lokale Einstellung merken
        saved["local"] = {**(saved.get("local") or {}), **{k: cur.get(k) for k in ("provider", "base_url", "default")}}
    saved["remote_model"], saved["fallback_model"] = remote_model, fallback_model
    local = {**LOCAL_DEFAULT, **{k: v for k, v in (saved.get("local") or {}).items() if v}}
    text = fb_block_set(text, fallback_model if mode == "auto" else None)
    data = yaml_rt.load(text)
    if mode == "remote":
        cfg_set(data, "model.provider", "anthropic")
        cfg_set(data, "model.default", remote_model)
        data["model"]["base_url"] = None  # nicht entfernen: daran haengen ~300 Kommentarzeilen
    else:
        base = ollama_base(to_plain(data))
        if not base:
            raise HTTPException(400, "Keine Ollama-Adresse eingestellt (Modell & Fallback → Lokales Modell).")
        if not local["default"]:
            raise HTTPException(400, "Kein lokales Modell gewählt (Modell & Fallback → Lokales Modell).")
        local = {**local, "base_url": base + "/v1"}
        for k in ("provider", "base_url", "default"):
            cfg_set(data, f"model.{k}", local[k])
    rn, fn = CLAUDE_MODELS[remote_model]["name"], CLAUDE_MODELS[fallback_model]["name"]
    ln = local["default"] or "lokales Modell"
    desc = {"local": f"Gehirn: Lokal (nur {ln}, keine Cloud)",
            "auto": f"Gehirn: Automatisch ({ln}, sonst {fn})",
            "remote": f"Gehirn: Remote ({rn}, PC wird geschont)"}[mode]
    privacy = "" if mode == "local" else "Datenschutz: Gesprächstexte gehen an Anthropic (USA). Audio bleibt lokal."
    return {"text": dump_rt(data), "desc": desc, "privacy": privacy, "saved": saved,
            "effect": ("Gilt ab der nächsten Frage, kein Neustart nötig. Das lokale Modell wird aus dem PC-Grafikspeicher entladen."
                       if mode == "remote" else "Gilt ab der nächsten Frage, kein Neustart nötig."),
            "ui": {"brain_mode": mode, "brain_model": remote_model if mode == "remote" else local["default"],
                   "brain_model_remote": remote_model if mode == "remote" else (fallback_model if mode == "auto" else None)}}


async def unload_local_model() -> bool:
    """Bei Remote: lokales Modell sofort aus dem PC-VRAM werfen (sonst haelt Ollama es noch keep_alive-Minuten)."""
    saved = load_brain_saved()
    local = {**LOCAL_DEFAULT, **{k: v for k, v in (saved.get("local") or {}).items() if v}}
    ollama = ollama_base()
    if not ollama or not local["default"] or llm_type() != "ollama":
        return False  # Vertrag §1.6: bei openai nichts entladen
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{ollama}/api/generate", json={"model": local["default"], "keep_alive": 0})
        return r.status_code < 400
    except httpx.HTTPError:
        return False  # PC aus: nichts zu entladen


def write_ui_fields(fields: dict, reason: str) -> None:
    ui = read_ui_settings()
    backup("ui", read_text(UI_SETTINGS), reason)
    for k, v in fields.items():
        if v is None:
            ui.pop(k, None)
        else:
            ui[k] = v
    atomic_write(UI_SETTINGS, json.dumps(ui, indent=2, ensure_ascii=False) + "\n")


@app.get("/api/brain")
async def brain_get():
    return brain_state()


@app.post("/api/brain/save")
async def brain_save(request: Request):
    d = await body(request)
    ch = build_brain_change(d.get("mode"), d.get("remote_model") or (d.get("claude") if d.get("mode") == "remote" else None),
                            d.get("fallback_model") or (d.get("claude") if d.get("mode") == "auto" else None))
    pv = make_preview("config", ch["text"], ch["desc"], ch["effect"], ch["privacy"])
    PENDING[pv["preview"]]["after_ui"] = ch["ui"]
    PENDING[pv["preview"]]["after_brain"] = ch["saved"]
    return pv


# ------------------------------------------------------------------ Interne Schnittstelle fuer nuorb-ui (Smartphone-Schalter)
INTERNAL_TOKEN_FILE = UI_SETTINGS.parent / "internal.token"
# Eigenes, enges Token für Werkzeuge des Sprachmodells (MCP „nuorb“ läuft im Hermes-Prozess): nur diese Pfade.
# Das volle interne Token (Gehirn umschalten, Oberfläche einstellen) bleibt bei Oberfläche und Admin-Panel.
MCP_TOKEN_FILE = UI_SETTINGS.parent / "mcp.token"
MCP_PATHS = {"/internal/assistant"}


def ensure_internal_token() -> None:
    for f in (INTERNAL_TOKEN_FILE, MCP_TOKEN_FILE):
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(f, secrets.token_hex(32) + "\n", 0o600)


def _token_matches(f: Path, given: str) -> bool:
    try:
        tok = f.read_text().strip()
    except FileNotFoundError:
        return False
    return bool(tok) and bool(given) and hmac.compare_digest(given.strip(), tok)


def internal_token_ok(given: str, path: str = "") -> bool:
    """Volles internes Token: alle /internal/-Pfade. MCP-Token: nur MCP_PATHS."""
    return _token_matches(INTERNAL_TOKEN_FILE, given) or (path in MCP_PATHS and _token_matches(MCP_TOKEN_FILE, given))


BRAIN_LOCK = asyncio.Lock()


@app.get("/internal/brain")
async def internal_brain_get():
    st = brain_state()
    st["anthropic_key"] = bool(env_value("ANTHROPIC_API_KEY"))
    return st


@app.post("/internal/brain")
async def internal_brain_set(request: Request):
    """Nur Gehirn-Modus und Claude-Modell. Gleiche Logik wie im Admin-UI, aber ohne Vorschau (der Nutzer hat am
    Smartphone bestaetigt): Sicherung, atomar schreiben, `hermes config check`, bei Fehler automatisch zurueck."""
    d = await body(request)
    mode = d.get("mode")
    model = d.get("claude") or d.get("model")
    src = re.sub(r"[^a-z0-9\-]", "", str(d.get("source") or "handy"))[:20] or "handy"
    async with BRAIN_LOCK:
        ch = build_brain_change(mode, model if mode == "remote" else None, model if mode == "auto" else None)
        path = FILES["config"][0]
        old = read_text(path)
        if ch["text"] == old:
            st = brain_state()
            return {"ok": True, "unchanged": True, "mode": st["mode"], "claude": st["claude"],
                    "effect": "Ist bereits so eingestellt.", "privacy": ch["privacy"]}
        bid = backup("config", old, f"{ch['desc']} (via {src})")
        atomic_write(path, ch["text"])
        code, out = await run_hermes("config", "check", timeout=60)
        if code != 0:
            atomic_write(path, old)
            audit(f"internal:{src}", f"Gehirn-Wechsel zurückgenommen (config check fehlgeschlagen): {ch['desc']}")
            raise HTTPException(500, "Umschalten fehlgeschlagen, alte Einstellung wiederhergestellt.")
        save_brain_saved(ch["saved"])
        write_ui_fields(ch["ui"], f"Gehirn-Modus via {src}")
        audit(f"internal:{src}", f"{ch['desc']} (Sicherung {bid})")
        if mode == "remote":
            await unload_local_model()
    st = brain_state()
    return {"ok": True, "mode": st["mode"], "claude": st["claude"], "active_model": st["active_model"],
            "effect": ch["effect"], "privacy": ch["privacy"], "backup": bid}


@app.post("/api/brain/projekt")
async def brain_projekt(request: Request):
    d = await body(request)
    model = d.get("model")
    if model not in CLAUDE_MODELS:
        raise HTTPException(400, "Ungültiges Modell.")
    text = read_text(FILES["config"][0])
    m = PC_BLOCK.search(text)
    if not m:
        raise HTTPException(404, "Projekt-Claude ist nicht eingerichtet.")
    block = re.sub(r"(CLAUDE_MODELL:\s*)\S+", lambda x: x.group(1) + model, m.group(0))
    block = re.sub(r"Claude (Haiku|Sonnet|Opus) 5\.5", CLAUDE_MODELS[model]["name"], block)
    return make_preview("config", text[:m.start()] + block + text[m.end():], f"Projekt-Claude: {CLAUDE_MODELS[model]['name']}",
                        "Wirkt nach Neustart des Hermes-Gateways.")


# ------------------------------------------------------------------ Persönlichkeit, Gedächtnis, UI-Einstellungen
def read_ui_settings() -> dict:
    try:
        return json.loads(UI_SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


@app.get("/api/persona")
async def persona_get():
    ui = read_ui_settings()
    return {"soul": read_text(FILES["soul"][0]), "ui_persona": ui.get("persona"),
            "ui_supported": UI_SETTINGS.exists(), "assistant_name": ui_field_value(ui, "assistant_name"),
            "onboarded": ui_field_value(ui, "onboarded"),
            **{k: v for k, v in ui_brand().items() if k in ("brand_name", "wake_phrase", "ui_lang")}}


@app.post("/api/persona/save")
async def persona_save(request: Request):
    d = await body(request)
    if "soul" in d:
        text = str(d["soul"])
        if len(text) > 20000:
            raise HTTPException(400, "SOUL.md ist zu lang (max. 20.000 Zeichen).")
        return make_preview("soul", text if text.endswith("\n") or not text else text + "\n",
                            "Persönlichkeit (SOUL.md) ändern",
                            "Gilt ab der nächsten Hermes-Sitzung. Die erste Antwort danach ist einmalig ~8 s langsamer (Prompt-Cache).")
    if "ui_persona" in d:
        ui = read_ui_settings()
        val = str(d["ui_persona"]).strip()
        if len(val) > 4000:
            raise HTTPException(400, "Die Sprach-Persona darf höchstens 4000 Zeichen haben.")
        if val:
            ui["persona"] = val
        else:
            ui.pop("persona", None)
        return make_preview("ui", json.dumps(ui, indent=2, ensure_ascii=False) + "\n", "Sprach-Persona des Smartphones ändern",
                            "Gilt für die nächste Frage am Smartphone. Erste Antwort danach einmalig langsamer.")
    if "assistant_name" in d:
        ui = read_ui_settings()
        v = ui_field_check("assistant_name", d["assistant_name"])
        if v == ASSISTANT_NAME:
            ui.pop("assistant_name", None)  # Standard = Marke (haelt den Prompt-Cache)
        else:
            ui["assistant_name"] = v
        return make_preview("ui", json.dumps(ui, indent=2, ensure_ascii=False) + "\n", f"Name des Assistenten: „{v}“",
                            f"Gilt ab der nächsten Frage. Das Weckwort bleibt „{WAKE_PHRASE}“. "
                            "Erste Antwort danach einmalig langsamer (Prompt-Cache).")
    if d.get("onboarded") is False:
        ui = read_ui_settings()
        ui["onboarded"] = False
        return make_preview("ui", json.dumps(ui, indent=2, ensure_ascii=False) + "\n", "Begrüßung beim nächsten Öffnen wiederholen",
                            "Beim nächsten Öffnen der Oberfläche stellt sich der Assistent wieder vor.")
    raise HTTPException(400, "Nichts zu speichern.")


MEM_SEP = "\n§\n"


@app.get("/api/memory")
async def memory_get():
    cfg = to_plain(load_config_rt()).get("memory") or {}

    def entries(key):
        t = read_text(FILES[key][0]).strip()
        return [e.strip() for e in re.split(r"\n?^§\s*$\n?", t, flags=re.M) if e.strip()] if t else []

    return {"memory": entries("memory"), "user": entries("user"),
            "limits": {"memory": cfg.get("memory_char_limit", 2200), "user": cfg.get("user_char_limit", 1375)},
            "enabled": {"memory": cfg.get("memory_enabled", True), "user": cfg.get("user_profile_enabled", True)}}


@app.post("/api/memory/save")
async def memory_save(request: Request):
    d = await body(request)
    key = d.get("which")
    if key not in ("memory", "user"):
        raise HTTPException(400, "Unbekannt.")
    items = [str(x).strip().replace("\n§\n", "\n") for x in d.get("entries", []) if str(x).strip()]
    text = MEM_SEP.join(items) + ("\n" if items else "")
    return make_preview(key, text, f"{label_of(key)} bearbeiten",
                        "Gilt ab der nächsten Hermes-Sitzung (Hermes liest das Gedächtnis beim Sitzungsstart).")


@app.get("/api/ui-settings")
async def ui_settings_get():
    voices = sorted(p.stem for p in (HERMES_HOME / "cache/piper-voices").glob("*.onnx"))
    return {"settings": read_ui_settings(), "exists": UI_SETTINGS.exists(), "voices": voices, "wake_phrase": WAKE_PHRASE}


@app.post("/api/ui-settings/save")
async def ui_settings_save(request: Request):
    d = await body(request)
    ui = read_ui_settings()
    allowed = {"piper_voice": str, "length_scale": float, "reasoning_voice": bool, "mode": str}
    for k in INTERNAL_UI_FIELDS:
        if k in d and d[k] is not None and d[k] != "":
            ui[k] = ui_field_check(k, d[k])
    for k, t in allowed.items():
        if k in d:
            v = d[k]
            if v is None or v == "":
                ui.pop(k, None)
                continue
            if t is bool and not isinstance(v, bool):
                raise HTTPException(400, f"{k} muss true oder false sein.")
            v = t(v)
            if k == "length_scale" and not 0.6 <= v <= 1.6:
                raise HTTPException(400, "Sprechtempo zwischen 0,6 und 1,6.")
            if k == "mode" and v not in ("live", "demo"):
                raise HTTPException(400, "Modus live oder demo.")
            if k == "piper_voice" and not re.fullmatch(r"[A-Za-z0-9_\-]+", v):
                raise HTTPException(400, "Ungültige Stimme.")
            ui[k] = v
    return make_preview("ui", json.dumps(ui, indent=2, ensure_ascii=False) + "\n", "Spracheinstellungen des Smartphones",
                        "Gilt für die nächste Antwort am Smartphone.")


# Interne Schnittstelle fuer einzelne ui.json-Schalter (Smartphone). Whitelist: Feld -> (Typ, Bezeichnung, Standard, Bereich)
INTERNAL_UI_FIELDS = {
    "wake_word": (bool, f"Wecken mit „{WAKE_PHRASE}“", False, None),
    "follow_up": (bool, "Nach Antwort weiter zuhören", True, None),
    "follow_up_timeout_s": (int, "Wartezeit auf Antwort", 8, (4, 30)),
    "assistant_name": (str, "Name des Assistenten", ASSISTANT_NAME, NAME_RE),  # Bereich = Regex
    # Fehlt das Feld, begruesst die Instanz nie von selbst -> Standard "erledigt"
    "onboarded": (bool, "Begrüßung erledigt", True, None),
}


def ui_field_check(k: str, v):
    """Prueft Typ und Bereich eines Whitelist-Felds; bool ist kein int."""
    t, label, _, rng = INTERNAL_UI_FIELDS[k]
    if not isinstance(v, t) or (t is not bool and isinstance(v, bool)):
        raise HTTPException(400, f"{k} hat den falschen Typ.")
    if t is str:
        v = v.strip()
        if not re.fullmatch(rng, v):
            raise HTTPException(400, f"{label}: 1-24 Zeichen, mit Buchstaben beginnen, danach Buchstaben, Ziffern, "
                                     "Leerzeichen, Punkt oder Bindestrich.")
        return v
    if rng and not rng[0] <= v <= rng[1]:
        raise HTTPException(400, f"{label}: erlaubt {rng[0]}-{rng[1]}.")
    return v


def ui_field_value(ui: dict, k: str):
    return ui.get(k, INTERNAL_UI_FIELDS[k][2])


def ui_field_text(k: str, v) -> str:
    label = INTERNAL_UI_FIELDS[k][1]
    if isinstance(v, str):
        return f"{label}: „{v}“"
    if k == "onboarded":
        return f"{label}: {'ja' if v else 'nein (Begrüßung beim nächsten Öffnen)'}"
    return f"{label}: {'an' if v is True else 'aus' if v is False else v}" + (" s" if k.endswith("_s") else "")


@app.get("/internal/ui")
async def internal_ui_get():
    ui = read_ui_settings()
    return {k: ui_field_value(ui, k) for k in INTERNAL_UI_FIELDS}


@app.post("/internal/ui")
async def internal_ui_set(request: Request):
    d = await body(request)
    src = re.sub(r"[^a-z0-9\-]", "", str(d.get("source") or "handy"))[:20] or "handy"
    fields = {}
    for k, v in d.items():
        if k == "source":
            continue
        if k not in INTERNAL_UI_FIELDS:
            raise HTTPException(400, f"Feld {k} ist hier nicht erlaubt.")
        fields[k] = ui_field_check(k, v)
    if not fields:
        raise HTTPException(400, "Nichts zu ändern.")
    async with BRAIN_LOCK:  # schuetzt auch ui.json vor gleichzeitigen Schreibern
        ui = read_ui_settings()
        changed = {k: v for k, v in fields.items() if ui_field_value(ui, k) != v}
        if changed:
            write_ui_fields(changed, f"Smartphone-Schalter via {src}")
            audit(f"internal:{src}", ", ".join(ui_field_text(k, v) for k, v in changed.items()))
    ui = read_ui_settings()
    return {"ok": True, **({"unchanged": True} if not changed else {}), **{k: ui_field_value(ui, k) for k in fields}}


@app.post("/api/tts-test")
async def tts_test(request: Request):
    d = await body(request)
    text = str(d.get("text", "Guten Tag. Alle Systeme sind bereit."))[:300]
    payload = {"text": text}
    # Probehoeren mit den Formularwerten (noch nicht gespeichert); nur vorhandene Stimmen
    if d.get("piper_voice"):
        v = str(d["piper_voice"])
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", v) or not (HERMES_HOME / f"cache/piper-voices/{v}.onnx").is_file():
            raise HTTPException(400, "Unbekannte Stimme.")
        payload["voice"] = v
    if d.get("length_scale") is not None:
        try:
            ls = float(d["length_scale"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Sprechtempo ungültig.")
        if not 0.6 <= ls <= 1.6:
            raise HTTPException(400, "Sprechtempo zwischen 0,6 und 1,6.")
        payload["length_scale"] = ls
    try:
        async with httpx.AsyncClient(timeout=20, verify=ui_ssl_ctx()) as c:
            r = await c.post(f"{UI_URL}/api/tts", json=payload)
        if r.status_code >= 400:
            raise HTTPException(502, "Sprachausgabe nicht erreichbar.")
        return Response(r.content, media_type=r.headers.get("content-type", "audio/wav"))
    except httpx.HTTPError:
        raise HTTPException(502, "Sprachausgabe nicht erreichbar.")


# ------------------------------------------------------------------ Skills
def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    try:
        d = YAML(typ="safe").load(m.group(1))
        return d if isinstance(d, dict) else {}
    except YAMLError:
        return {}


# Skills, die ein eigenes MCP-Werkzeug brauchen: an/aus schaltet beides zusammen.
SKILL_MCP = {"zuhause": ["zuhause", "zuhause_schalten"]}
# Nur lesende Tests: Skill -> (Befehl ohne Thema, erlaubte Themen)
SKILL_PROBE = {"zuhause": (["/usr/bin/python3", str(HERMES_HOME / "projekt-claude/zuhause_mcp.py"), "--satz"],
                           ["wetter", "heizung", "raeume", "klima", "energie"])}
# Zugangsdaten-Dateien: nur „hinterlegt ja/nein“ wird angezeigt, nie der Inhalt.
SKILL_ACCESS = {"zuhause": (HERMES_HOME / "zuhause.env", ("DASHBOARD_USER", "DASHBOARD_PASS"))}


def skill_mcp_state(text: str, names: list[str]):
    """True/False wenn alle eingetragenen Server gleich stehen, "gemischt" sonst, None wenn keiner eingetragen ist."""
    st = [v for v in (mcp_enabled_in(text, n) for n in names) if v is not None]
    return None if not st else st[0] if len(set(st)) == 1 else "gemischt"


def skill_access_ok(name: str) -> bool | None:
    if name not in SKILL_ACCESS:
        return None
    f, keys = SKILL_ACCESS[name]
    try:
        vals = dict(re.findall(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$", f.read_text(), re.M))
    except OSError:
        return False
    return all(vals.get(k, "").strip("'\"") for k in keys)


def scan_skills() -> list[dict]:
    root = HERMES_HOME / "skills"
    cfg_text = read_text(FILES["config"][0])
    disabled = set((to_plain(yaml_rt.load(cfg_text) or {}).get("skills") or {}).get("disabled") or [])
    out = []
    for f in sorted(root.glob("**/SKILL.md")):
        rel = f.parent.relative_to(root)
        if any(p.startswith(".") for p in rel.parts):
            continue
        fm = parse_frontmatter(read_text(f))
        name = str(fm.get("name") or f.parent.name)
        cat = rel.parts[0] if len(rel.parts) > 1 else ""
        meta = (fm.get("metadata") or {}).get("hermes") or {} if isinstance(fm.get("metadata"), dict) else {}
        out.append({"name": name, "dir": str(rel), "category": cat, "description": str(fm.get("description") or ""),
                    "author": fm.get("author"), "version": fm.get("version"),
                    "tags": meta.get("tags") or [], "enabled": name not in disabled and f.parent.name not in disabled,
                    "own": (f.parent / ".nuorb-admin").exists(),
                    "editable": len(rel.parts) == 2, "size": f.stat().st_size,
                    "mcp": SKILL_MCP.get(name), "mcp_enabled": skill_mcp_state(cfg_text, SKILL_MCP[name]) if name in SKILL_MCP else None,
                    "mcp_present": [n for n in SKILL_MCP.get(name, []) if mcp_enabled_in(cfg_text, n) is not None],
                    "probe": SKILL_PROBE[name][1] if name in SKILL_PROBE else None,
                    "access": skill_access_ok(name)})
    return out


@app.get("/api/skills")
async def skills_list():
    items = scan_skills()
    cats = sorted({s["category"] for s in items if s["category"]})
    return {"items": items, "categories": cats}


@app.get("/api/skills/content")
async def skill_content(dir: str):
    root = HERMES_HOME / "skills"
    p = (root / dir / "SKILL.md").resolve()
    if root.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "Skill nicht gefunden.")
    return {"text": read_text(p), "dir": dir}


@app.post("/api/skills/toggle")
async def skills_toggle(request: Request):
    d = await body(request)
    name = str(d.get("name", ""))
    enable = bool(d.get("enabled"))
    if name not in {s["name"] for s in scan_skills()}:
        raise HTTPException(404, "Skill nicht gefunden.")
    data = load_config_rt()
    dis = set((to_plain(data).get("skills") or {}).get("disabled") or [])
    was = name not in dis
    dis.discard(name) if enable else dis.add(name)
    if was != enable:
        cfg_set(data, "skills.disabled", sorted(dis))
    text = dump_rt(data)
    desc, effect = f"Skill {name} {'aktivieren' if enable else 'deaktivieren'}", "Gilt ab der nächsten Hermes-Sitzung."
    todo = [n for n in SKILL_MCP.get(name, []) if mcp_enabled_in(text, n) not in (None, enable)]
    for n in todo:
        text = mcp_set_enabled(text, n, enable)
    if todo:
        desc += f" (mit MCP-Werkzeug {', '.join(todo)})"
        effect += f" Die Werkzeuge werden innerhalb ~1 Minute {'verbunden' if enable else 'getrennt'}."
    return make_preview("config", text, desc, effect)


@app.post("/api/skills/probe")
async def skills_probe(request: Request):
    """Nur lesender Funktionstest eines Skills mit Werkzeug (z. B. Wetterstation: aktueller Satz + Rohwerte)."""
    d = await body(request)
    probe = SKILL_PROBE.get(str(d.get("name", "")))
    if not probe:
        raise HTTPException(404, "Für diesen Skill gibt es keinen Test.")
    topic = str(d.get("topic") or probe[1][0])
    if topic not in probe[1]:
        raise HTTPException(400, "Unbekanntes Thema.")
    cmd = [*probe[0], topic]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(HOME))
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 25)
    except asyncio.TimeoutError:
        p.kill()
        raise HTTPException(504, "Keine Antwort innerhalb von 25 s.")
    text = redact_text(out.decode(errors="replace"))
    sentence = text.split("\n\n")[0].strip()
    raw = None
    if (m := re.search(r"^Rohwerte:\s*(\{.*\})\s*$", text, re.M)):
        with contextlib.suppress(ValueError):
            raw = json.loads(m.group(1))
    return {"ok": p.returncode == 0, "sentence": sentence, "raw": raw, "output": text[-3000:]}


def slug(s: str) -> str:
    s = s.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60]


@app.post("/api/skills/save")
async def skills_save(request: Request):
    """Neuen Skill anlegen oder bestehenden SKILL.md-Text ersetzen."""
    d = await body(request)
    if d.get("dir"):
        parts = str(d["dir"]).split("/")
        if len(parts) != 2:
            raise HTTPException(400, "Nur Skills der Form Kategorie/Name sind hier bearbeitbar.")
        key = f"skill:{parts[0]}/{parts[1]}"
        text = str(d.get("text", ""))
        return make_preview(key, text, f"Skill {parts[1]} bearbeiten", "Gilt ab der nächsten Hermes-Sitzung.")
    name = slug(str(d.get("name", "")))
    cat = slug(str(d.get("category", "") or "nuorb"))
    desc = " ".join(str(d.get("description", "")).split())
    body_text = str(d.get("body", "")).strip()
    if not name or not desc or not body_text:
        raise HTTPException(400, "Name, Beschreibung und Anleitung sind Pflicht.")
    if len(desc) > 300:
        raise HTTPException(400, "Die Beschreibung ist zu lang (max. 300 Zeichen).")
    if any(s["name"] == name for s in scan_skills()):
        raise HTTPException(409, f"Es gibt schon einen Skill namens {name}.")
    fm = dict(name=name, description=desc, version="1.0.0", author="NuOrb-Verwaltung")
    buf = io.StringIO()
    YAML().dump(fm, buf)
    text = f"---\n{buf.getvalue()}---\n\n# {d.get('title') or name}\n\n{body_text}\n"
    pv = make_preview(f"skill:{cat}/{name}", text, f"Neuen Skill {name} anlegen",
                      "Gilt ab der nächsten Hermes-Sitzung.")
    PENDING[pv["preview"]]["mark_own"] = True
    return pv


@app.post("/api/skills/delete")
async def skills_delete(request: Request):
    d = await body(request)
    parts = str(d.get("dir", "")).split("/")
    if len(parts) != 2:
        raise HTTPException(400, "Unbekannter Skill.")
    p = HERMES_HOME / "skills" / parts[0] / parts[1]
    if not (p / "SKILL.md").exists():
        raise HTTPException(404, "Skill nicht gefunden.")
    extra = [x for x in p.iterdir() if x.name not in ("SKILL.md", ".nuorb-admin")]
    if extra:
        raise HTTPException(409, "Der Skill hat weitere Dateien. Bitte stattdessen deaktivieren.")
    return make_preview(f"skill:{parts[0]}/{parts[1]}", None, f"Skill {parts[1]} löschen",
                        "Die Sicherung bleibt erhalten und lässt sich wiederherstellen.")


# ------------------------------------------------------------------ Bestätigungsregeln
@app.get("/api/approvals")
async def approvals_get():
    a = to_plain(load_config_rt()).get("approvals") or {}
    return {"mode": a.get("mode", "smart"), "timeout": a.get("timeout", 300), "cron_mode": a.get("cron_mode", "deny"),
            "unattended_mode": a.get("unattended_mode", "deny"), "deny": a.get("deny") or []}


@app.post("/api/approvals/save")
async def approvals_save(request: Request):
    d = await body(request)
    ch = {}
    if d.get("mode") in ("manual", "smart", "off"):
        ch["approvals.mode"] = d["mode"]
    if "timeout" in d:
        t = int(d["timeout"])
        if not 15 <= t <= 3600:
            raise HTTPException(400, "Wartezeit zwischen 15 und 3600 s.")
        ch["approvals.timeout"] = t
    for k in ("cron_mode", "unattended_mode"):
        if d.get(k) in ("deny", "approve"):
            ch[f"approvals.{k}"] = d[k]
    if "deny" in d:
        pats = [str(x).strip() for x in d["deny"] if str(x).strip()]
        ch["approvals.deny"] = pats or None
    danger = ""
    if ch.get("approvals.mode") == "off" or "approve" in (ch.get("approvals.cron_mode"), ch.get("approvals.unattended_mode")):
        danger = "Achtung: Damit führt NUORB gefährliche Befehle (z. B. Löschen) ohne Rückfrage aus."
    return config_preview(ch, "Bestätigungsregeln ändern", danger=danger)


# ------------------------------------------------------------------ MCP-Server
# ------------------------------------------------------------------ Smart Home (Home Assistant + ioBroker)
# Modul der Backend-Sitzung (scripts/backend-smarthome-mcp.py). Laeuft vom installierten Ort (~/.hermes/projekt-claude,
# Install-Skript) oder aus der Verwaltung selbst (modules/, kommt mit deploy.sh bzw. dem Image).
# Block `# >>> nuorb-smarthome` in mcp_servers: smarthome (lesen, trust full) + smarthome_schalten (trust untrusted ->
# Bestaetigungskarte). Zugang in smarthome.env (600): nie anzeigen. Ein abgeschaltetes System steht dort als `# aus: …`.
SH_INSTALLED = Path(os.environ.get("NUORB_SMARTHOME_MCP", "") or HERMES_HOME / "projekt-claude/smarthome_mcp.py")
SH_BUNDLED = BASE / "modules/smarthome_mcp.py"
SH_SERVERS = ["smarthome", "smarthome_schalten"]
SH_MASK = frozenset({"HA_TOKEN", "IOB_USER", "IOB_PASS", "IOB_TOKEN"})
SH_SYS = {"ha": ("HA_URL", "HA_TOKEN"), "iob": ("IOB_URL", "IOB_API", "IOB_USER", "IOB_PASS", "IOB_TOKEN")}
SH_BLOCK = re.compile(r"^  # >>> nuorb-smarthome[^\n]*\n.*?^  # <<< nuorb-smarthome[^\n]*\n", re.S | re.M)
SH_OFF = "# aus: "


def sh_script() -> Path | None:
    return next((p for p in (SH_INSTALLED, SH_BUNDLED) if p.exists()), None)


def sh_read() -> tuple[dict, dict]:
    """(Werte, an) – an[ha|iob] = System ist eingetragen und nicht abgeschaltet."""
    vals, off = {}, set()
    with contextlib.suppress(OSError):
        for line in FILES["smarthome"][0].read_text().splitlines():
            dis = line.startswith(SH_OFF)
            m = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$", line[len(SH_OFF):] if dis else line)
            if m and (dis or not line.lstrip().startswith("#")):
                vals[m.group(1)] = m.group(2).strip("'\"")
                if dis:
                    off.add(m.group(1))
    on = {k: bool(vals.get(keys[0])) and keys[0] not in off for k, keys in SH_SYS.items()}
    return vals, on


def sh_url(raw: str, label: str) -> str:
    s = raw.strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or re.search(r"\s", s):
        raise HTTPException(400, f"{label}: bitte eine Adresse wie http://homeassistant.local:8123 eingeben.")
    return s


async def sh_probe() -> tuple[bool, str]:
    script = sh_script()
    if not script:
        raise HTTPException(409, "Das Smart-Home-Modul fehlt auf diesem Server.")
    if not FILES["smarthome"][0].exists():
        raise HTTPException(409, "Noch kein Smart-Home-Zugang gespeichert.")
    if not any(sh_read()[1].values()):
        raise HTTPException(409, "Kein Smart-Home-System eingeschaltet.")
    cmd = ["/usr/bin/python3", str(script), "--satz", "raeume", "{}", "--zugang", str(FILES["smarthome"][0])]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(HOME))
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 25)
    except asyncio.TimeoutError:
        p.kill()
        raise HTTPException(504, "Das Smart-Home-System hat nicht rechtzeitig geantwortet.")
    text = redact_text(out.decode(errors="replace")).strip()
    for k, v in sh_read()[0].items():
        if k in SH_MASK and len(v) >= 4:
            text = text.replace(v, "•••")
    return p.returncode == 0, text[:2000]


def sh_block(script: Path, enabled: bool) -> str:
    """Block im Format von scripts/backend-smarthome-install.sh."""
    z, e = FILES["smarthome"][0], "true" if enabled else "false"
    return (f"  # >>> nuorb-smarthome (NuOrb-Verwaltung)\n"
            f"  smarthome:\n    command: /usr/bin/python3\n    enabled: {e}\n    args: [{script}, lesen, --zugang, {z}]\n"
            f"    trust: full\n    timeout: 25\n    tools:\n      resources: false\n      prompts: false\n"
            f"  smarthome_schalten:\n    command: /usr/bin/python3\n    enabled: {e}\n    args: [{script}, schalten, --zugang, {z}]\n"
            f"    trust: untrusted\n    timeout: 25\n    supports_parallel_tool_calls: false\n    tools:\n      resources: false\n      prompts: false\n"
            f"  # <<< nuorb-smarthome\n")


def sh_insert(text: str, block: str) -> str:
    if re.search(r"^  smarthome(_schalten)?:", text, re.M):
        raise HTTPException(409, "Es gibt schon einen MCP-Server smarthome außerhalb des markierten Blocks. Bitte unter „MCP-Server“ bereinigen.")
    kopf = re.compile(r"^mcp_servers:[ \t]*(\{\}|null|~)?[ \t]*$", re.M).search(text)
    if kopf:
        return text[:kopf.start()] + "mcp_servers:\n" + block + text[kopf.end() + 1:]
    if re.search(r"^mcp_servers:", text, re.M):
        raise HTTPException(409, "mcp_servers hat eine ungewohnte Form. Bitte im Expertenmodus prüfen.")
    return text.rstrip("\n") + "\nmcp_servers:\n" + block


@app.get("/api/smarthome")
async def smarthome_get():
    text = read_text(FILES["config"][0])
    v, on = sh_read()
    cfg = to_plain(yaml_rt.load(text) or {})
    ts = ((cfg.get("tools") or {}).get("tool_search") or {}).get("enabled")
    return {"installed": sh_script() is not None, "block": bool(SH_BLOCK.search(text)),
            "servers": {n: mcp_enabled_in(text, n) for n in SH_SERVERS}, "enabled": skill_mcp_state(text, SH_SERVERS),
            "access": {"exists": FILES["smarthome"][0].exists(), "ha_on": on["ha"], "iob_on": on["iob"],
                       "ha_url": v.get("HA_URL", ""), "ha_token": bool(v.get("HA_TOKEN")),
                       "iob_url": v.get("IOB_URL", ""), "iob_user": bool(v.get("IOB_USER")), "iob_pass": bool(v.get("IOB_PASS")),
                       "iob_token": bool(v.get("IOB_TOKEN"))},
            "tool_search": ts if isinstance(ts, str) else ("off" if ts is False else "auto" if ts is None else str(ts)),
            "brain_mode": brain_state()["mode"]}


@app.post("/api/smarthome/access")
async def smarthome_access(request: Request):
    d = await body(request)
    old, on = sh_read()
    new = dict(old)
    for k, label in (("HA_URL", "Home Assistant"), ("IOB_URL", "ioBroker")):
        if k.lower() in d:
            new[k] = sh_url(str(d[k.lower()] or ""), label)
    for k in ("HA_TOKEN", "IOB_USER", "IOB_PASS", "IOB_TOKEN"):
        val = str(d.get(k.lower()) or "")
        if val:  # leer = unveraendert
            if "\n" in val or len(val) > 4096:
                raise HTTPException(400, "Ungültiger Wert.")
            new[k] = val
    for sysk in ("ha", "iob"):
        if f"{sysk}_on" in d:
            on[sysk] = bool(d[f"{sysk}_on"])
    for sysk, keys in SH_SYS.items():
        if not new.get(keys[0]):
            on[sysk] = False
    if new.get("IOB_URL"):
        new["IOB_API"] = new.get("IOB_API") or "rest-api"
    if on["ha"] and not new.get("HA_TOKEN"):
        raise HTTPException(400, "Für Home Assistant fehlt das Zugriffstoken.")
    if on["iob"] and not (new.get("IOB_TOKEN") or (new.get("IOB_USER") and new.get("IOB_PASS"))):
        raise HTTPException(400, "Für ioBroker fehlen Benutzer und Passwort (oder ein Token).")
    lines = ["# NuOrb Smart Home: Zugang (von der NuOrb-Verwaltung geschrieben, nicht weitergeben)"]
    for sysk, title in (("ha", "Home Assistant"), ("iob", "ioBroker (Adapter rest-api)")):
        keys = [k for k in SH_SYS[sysk] if new.get(k)]
        if keys:
            lines.append(f"# {title}" + ("" if on[sysk] else " (ausgeschaltet)"))
            lines += [("" if on[sysk] else SH_OFF) + f"{k}={new[k]}" for k in keys]
    return make_preview("smarthome", "\n".join(lines) + "\n", "Smart-Home-Zugang speichern",
                        "Gilt sofort für „Verbindung testen“ und beim nächsten Werkzeug-Aufruf.")


@app.post("/api/smarthome/test")
async def smarthome_test(request: Request):
    ok, text = await sh_probe()
    audit(who(request), f"Smart Home: Verbindungstest {'ok' if ok else 'fehlgeschlagen'}")
    return {"ok": ok, "output": text}


@app.post("/api/smarthome/toggle")
async def smarthome_toggle(request: Request):
    d = await body(request)
    enable = bool(d.get("enabled"))
    text = read_text(FILES["config"][0])
    present = all(mcp_enabled_in(text, n) is not None for n in SH_SERVERS)
    if enable:
        ok, out = await sh_probe()
        if not ok:
            raise HTTPException(409, f"Verbindungstest fehlgeschlagen, Smart Home bleibt aus: {out[:300]}")
    if not present:
        if not enable:
            raise HTTPException(409, "Keine Änderung: Smart Home ist nicht eingetragen.")
        text = sh_insert(text, sh_block(sh_script(), True))
    else:
        for n in SH_SERVERS:
            if mcp_enabled_in(text, n) != enable:
                text = mcp_set_enabled(text, n, enable)
    return make_preview("config", text, f"Smart Home {'einschalten' if enable else 'ausschalten'}",
                        f"Hermes {'verbindet' if enable else 'trennt'} die Werkzeuge innerhalb ~1 Minute. Schalten fragt immer per Karte nach.")


@app.get("/api/mcp")
async def mcp_get():
    servers = to_plain(load_config_rt()).get("mcp_servers") or {}
    out = []
    for name, s in servers.items():
        s = s or {}
        out.append({"name": name, "enabled": s.get("enabled", True), "command": s.get("command"),
                    "args": s.get("args") or [], "url": s.get("url"),
                    "env": {k: (mask(v) if env_is_secret(k) else v) for k, v in (s.get("env") or {}).items()},
                    "tools": s.get("tools")})
    return {"items": out}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def mcp_section(lines: list[str]) -> tuple[int, int]:
    """Zeilenbereich von `mcp_servers:` (Kopfzeile, Ende exkl.). Zeilenweise statt ruamel, weil Marker-Kommentare
    (Projekt-Claude, Claude-Fallback) sonst an falsche Schluessel wandern. Ende = erste Zeile auf Spalte 0."""
    starts = [i for i, l in enumerate(lines) if re.match(r"^mcp_servers:", l)]
    if not starts:
        return -1, -1
    if len(starts) > 1:
        raise HTTPException(409, "mcp_servers kommt mehrfach vor.")
    s = starts[0]
    e = s + 1
    while e < len(lines) and (not lines[e].strip() or _indent(lines[e]) > 0):
        e += 1
    while e - 1 > s and not lines[e - 1].strip():  # Leerzeilen am Ende gehoeren nicht dazu
        e -= 1
    return s, e


def mcp_entry_span(lines: list[str], name: str) -> tuple[int, int]:
    s, e = mcp_section(lines)
    if s < 0:
        raise HTTPException(404, "Keine MCP-Server eingetragen.")
    for i in range(s + 1, e):
        if re.match(rf"^  {re.escape(name)}:\s*(#.*)?$", lines[i]):
            j = i + 1
            while j < e and (not lines[j].strip() or _indent(lines[j]) > 2):
                j += 1
            while j - 1 > i and not lines[j - 1].strip():
                j -= 1
            return i, j
    raise HTTPException(404, f"MCP-Server {name} nicht gefunden.")


def mcp_insert(lines: list[str], block: str) -> str:
    s, e = mcp_section(lines)
    if s < 0:
        return "".join(lines).rstrip("\n") + "\nmcp_servers:\n" + block
    head = lines[s]
    if re.match(r"^mcp_servers:\s*\{\s*\}\s*$", head):
        lines[s] = "mcp_servers:\n"
    elif re.match(r"^mcp_servers:\s*\{", head):
        raise HTTPException(409, "mcp_servers steht als Inline-Mapping. Bitte im Expertenmodus umstellen.")
    return "".join(lines[:e]) + block + "".join(lines[e:])


def mcp_set_enabled(text: str, name: str, enabled: bool) -> str:
    """`    enabled:` im Eintrag setzen (zeilenweise; Marker-Kommentare um den Eintrag bleiben erhalten)."""
    lines = text.splitlines(True)
    s, e = mcp_entry_span(lines, name)
    val = "true" if enabled else "false"
    for i in range(s + 1, e):
        if re.match(r"^    enabled:\s", lines[i]):
            lines[i] = f"    enabled: {val}\n"
            break
    else:  # wie die Install-Skripte der Sprachsteuerung: direkt unter `command:` (sonst unter dem Namen)
        at = next((i + 1 for i in range(s + 1, e) if re.match(r"^    command:", lines[i])), s + 1)
        lines.insert(at, f"    enabled: {val}\n")
    return "".join(lines)


def mcp_marked_by(lines: list[str], s: int) -> str | None:
    """Name des Skript-Markers, falls der Eintrag in einem markierten Block (`# >>> nuorb-…` … `# <<< nuorb-…`)
    steht – auch wenn mehrere Server im selben Block liegen."""
    for i in range(s - 1, -1, -1):
        if re.match(r"^\s*# <<< nuorb-", lines[i]) or re.match(r"^[^\s#]", lines[i]):
            return None
        if (m := re.match(r"^\s*# >>> (nuorb-[a-z0-9\-]+)(?:\s*\(([^)]*)\))?", lines[i])):
            return m.group(2) or m.group(1)
    return None


def mcp_enabled_in(text: str, name: str) -> bool | None:
    entry = (to_plain(yaml_rt.load(text) or {}).get("mcp_servers") or {}).get(name)
    return None if entry is None else entry.get("enabled", True) is not False


@app.post("/api/mcp/save")
async def mcp_save(request: Request):
    d = await body(request)
    name = str(d.get("name", "")).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,40}", name):
        raise HTTPException(400, "Name: nur Kleinbuchstaben, Ziffern, - und _.")
    action = d.get("action")
    text = read_text(FILES["config"][0])
    lines = text.splitlines(True)
    if action == "remove":
        s, e = mcp_entry_span(lines, name)
        if name == "claude":
            raise HTTPException(409, "Projekt-Claude wird über ‚Modell & Fallback‘ bzw. das Skript der Sprachsteuerung verwaltet.")
        if (owner := mcp_marked_by(lines, s)):
            raise HTTPException(409, f"{name} wird per Skript verwaltet ({owner}). Hier nur an/aus schalten.")
        return make_preview("config", "".join(lines[:s] + lines[e:]), f"MCP-Server {name} entfernen",
                            "Hermes trennt die Verbindung innerhalb ~1 Minute.")
    if action == "toggle":
        return make_preview("config", mcp_set_enabled(text, name, bool(d.get("enabled"))), f"MCP-Server {name} {'aktivieren' if d.get('enabled') else 'deaktivieren'}",
                            "Hermes übernimmt das innerhalb ~1 Minute.")
    if name in (to_plain(load_config_rt()).get("mcp_servers") or {}):
        raise HTTPException(409, f"Es gibt schon einen MCP-Server namens {name}.")
    entry = {}
    if d.get("url"):
        url = str(d["url"]).strip()
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or re.search(r"\s", url):
            raise HTTPException(400, "URL muss mit http:// oder https:// beginnen.")
        if u.username or u.password:   # Zugangsdaten gehören in die Kopfzeilen des MCP-Eintrags, nicht in die URL
            raise HTTPException(400, "Die URL darf keine Zugangsdaten enthalten.")
        entry["url"] = url
    elif d.get("command"):
        entry["command"] = str(d["command"]).strip()
        entry["args"] = [str(a) for a in (d.get("args") or [])]
    else:
        raise HTTPException(400, "Befehl oder URL angeben.")
    entry["enabled"] = bool(d.get("enabled", True))
    buf = io.StringIO()
    YAML().dump({name: entry}, buf)
    block = "".join("  " + l if l.strip() else l for l in buf.getvalue().splitlines(True))
    return make_preview("config", mcp_insert(lines, block), f"MCP-Server {name} eintragen",
                          "Hermes verbindet sich innerhalb ~1 Minute.",
                          danger="MCP-Server laufen mit den Rechten von Hermes. Nur vertrauenswürdige Server eintragen.")


# ------------------------------------------------------------------ Schlüssel (.env)
@app.get("/api/env")
async def env_get():
    out = []
    for n, v, _ in read_env():
        if n:
            secret = env_is_secret(n)
            out.append({"name": n, "secret": secret, "value": mask(v) if secret else v,
                        "protected": n in ENV_PROTECTED})
    return {"items": out}


@app.post("/api/env/save")
async def env_save(request: Request):
    d = await body(request)
    name = str(d.get("name", "")).strip().upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{1,60}", name):
        raise HTTPException(400, "Ungültiger Variablenname.")
    if name in ENV_PROTECTED:
        raise HTTPException(403, "Diese Variable verbindet Hermes mit der Smartphone-Oberfläche und ist hier gesperrt.")
    remove = bool(d.get("remove"))
    value = str(d.get("value", ""))
    if not remove and (not value or "\n" in value or len(value) > 4096):
        raise HTTPException(400, "Wert fehlt oder ist ungültig.")
    lines, found = [], False
    for n, _v, raw in read_env():
        if n == name:
            found = True
            if not remove:
                lines.append(f"{name}={value}")
        else:
            lines.append(raw)
    if not found and not remove:
        lines.append(f"{name}={value}")
    if not found and remove:
        raise HTTPException(404, "Variable nicht vorhanden.")
    return make_preview("env", "\n".join(lines) + "\n", f"{name} {'entfernen' if remove else 'setzen'}",
                        "Wirkt nach Neustart des Hermes-Gateways.")


# ------------------------------------------------------------------ Zeitplaene (Hermes /api/jobs)
def hermes_auth() -> dict:
    k = env_value("API_SERVER_KEY")
    return {"Authorization": f"Bearer {k}"} if k else {}


async def hermes_api(method: str, path: str, js: dict | None = None):
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.request(method, f"{HERMES_URL}{path}", json=js, headers=hermes_auth())
    except httpx.HTTPError:
        raise HTTPException(502, "Hermes ist nicht erreichbar.")
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:500]}
    if r.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else None
        if isinstance(msg, dict):
            msg = msg.get("message")
        raise HTTPException(r.status_code, f"Hermes: {msg or data}")
    return data


@app.get("/api/jobs")
async def jobs_list():
    return await hermes_api("GET", "/api/jobs")


@app.post("/api/jobs")
async def jobs_create(request: Request):
    d = await body(request)
    js = {k: d[k] for k in ("name", "prompt", "schedule", "skills", "deliver") if d.get(k)}
    if not js.get("prompt") or not js.get("schedule"):
        raise HTTPException(400, "Auftrag und Zeitplan sind Pflicht.")
    audit(who(request), f"Zeitplan angelegt: {js.get('name') or js['schedule']}")
    return await hermes_api("POST", "/api/jobs", js)


@app.post("/api/jobs/{job_id}/{action}")
async def jobs_action(job_id: str, action: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", job_id) or action not in ("pause", "resume", "run", "delete", "update"):
        raise HTTPException(400, "Ungültig.")
    audit(who(request), f"Zeitplan {job_id}: {action}")
    if action == "delete":
        return await hermes_api("DELETE", f"/api/jobs/{job_id}")
    if action == "update":
        d = await body(request)
        js = {k: d[k] for k in ("name", "prompt", "schedule", "skills", "deliver") if k in d}
        return await hermes_api("PATCH", f"/api/jobs/{job_id}", js)
    return await hermes_api("POST", f"/api/jobs/{job_id}/{action}")


# ------------------------------------------------------------------ Sitzungen (Verlauf, nur lesend)
@app.get("/api/sessions")
async def sessions_list():
    try:
        return await hermes_api("GET", "/api/sessions?limit=40")
    except HTTPException as e:
        return {"error": e.detail, "sessions": []}


# ------------------------------------------------------------------ Dienste (root-Helfer über Path-Unit)
def ctl_result() -> dict | None:
    with contextlib.suppress(OSError, ValueError):
        return json.loads((CTL_DIR / "result.json").read_text())
    return None


@app.post("/api/services/{name}/restart")
async def service_restart(name: str, request: Request):
    if name not in UNITS:
        raise HTTPException(404, "Unbekannter Dienst.")
    if not CTL_DIR.is_dir():
        raise HTTPException(503, "Der Neustart-Helfer (nuorb-admin-ctl) ist nicht eingerichtet.")
    req = {"action": "restart", "unit": name, "id": secrets.token_hex(6), "time": now_iso()}
    audit(who(request), f"Neustart angefordert: {UNITS[name]}")
    atomic_write(CTL_DIR / "request", f"restart {name} {req['id']}\n", 0o640)
    if name == "admin":
        return {"ok": True, "id": req["id"], "self": True}
    for _ in range(60):
        await asyncio.sleep(0.5)
        r = ctl_result()
        if r and r.get("id") == req["id"]:
            return {"ok": r.get("ok", False), "id": req["id"], "result": r}
    return {"ok": False, "id": req["id"], "pending": True}


@app.get("/api/services")
async def services():
    return {"units": await asyncio.gather(*(unit_state(n) for n in UNITS)), "ctl": CTL_DIR.is_dir(),
            "last": ctl_result()}


# ------------------------------------------------------------------ Protokolle
LOG_SOURCES = {
    "agent": ("file", HERMES_HOME / "logs/agent.log", "Hermes: Agent"),
    "errors": ("file", HERMES_HOME / "logs/errors.log", "Hermes: Fehler"),
    "gateway": ("file", HERMES_HOME / "logs/gateway.log", "Hermes: Gateway"),
    "j-hermes": ("journal", UNITS["hermes"], "Dienst: Hermes-Gateway"),
    "j-stt": ("journal", UNITS["stt"], "Dienst: Spracherkennung"),
    "j-ui": ("journal", UNITS["ui"], "Dienst: Smartphone-Oberfläche"),
    "j-admin": ("journal", UNITS["admin"], "Dienst: Verwaltung"),
    "audit": ("file", DATA / "audit.log", "Verwaltung: Änderungsprotokoll"),
}


def tail(path: Path, lines: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, max(65536, lines * 400))
            f.seek(size - chunk)
            data = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return data.splitlines()[-lines:]


@app.get("/api/logs")
async def logs(source: str = "agent", lines: int = 300, level: str = "", q: str = ""):
    if source not in LOG_SOURCES:
        raise HTTPException(404, "Unbekannte Quelle.")
    kind, target, _ = LOG_SOURCES[source]
    lines = max(20, min(lines, 3000))
    if kind == "file":
        out = tail(target, lines * (4 if (level or q) else 1))
    else:
        p = await asyncio.create_subprocess_exec("journalctl", "-u", target, "-n", str(lines * (4 if (level or q) else 1)),
                                                 "--no-pager", "-o", "short-iso", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        o, _ = await p.communicate()
        out = o.decode("utf-8", "replace").splitlines()
    if level:
        lv = {"error": ("ERROR", "CRITICAL", "Traceback", "Fehler"), "warning": ("ERROR", "CRITICAL", "WARNING", "Traceback")}
        keys = lv.get(level, ())
        out = [l for l in out if any(k in l for k in keys)]
    if q:
        ql = q.lower()
        out = [l for l in out if ql in l.lower()]
    return {"lines": [redact_text(l)[:2000] for l in out[-lines:]],
            "sources": {k: v[2] for k, v in LOG_SOURCES.items()}}


# ------------------------------------------------------------------ Kiosk-Gerät (eingeschraenkter SSH-Schlüssel)
PHONE_CACHE: dict = {"t": 0, "data": None}
PHONE_CMDS = {"status", "reboot", "kiosk-reload", "bildschirm"}


async def phone_cmd(*args: str, timeout: float = 8) -> tuple[int, str]:
    if not PHONE_HOST:
        return 255, "Kein Kiosk-Gerät eingerichtet (PHONE_HOST fehlt)."
    if not PHONE_KEY.exists():
        return 255, "Kein Kiosk-Schlüssel eingerichtet."
    kh = BASE / "keys/known_hosts"
    cmd = ["ssh", "-i", str(PHONE_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
           "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={kh}", "-o", "IdentitiesOnly=yes",
           "-o", "LogLevel=ERROR", f"{PHONE_USER}@{PHONE_HOST}", " ".join(args)]
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                 stdin=asyncio.subprocess.DEVNULL)
        o, e = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode or 0, (o.decode("utf-8", "replace") or e.decode("utf-8", "replace")).strip()[:4000]
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        return 124, "Zeitüberschreitung"


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def phone_error_text(out: str, code: int) -> str:
    o = out.lower()
    if "host key" in o:
        return "Hostschlüssel des Kiosk-Geräts ist noch nicht bestätigt (Einrichtung läuft)."
    if "permission denied" in o:
        return "Die Steuerung ist auf dem Kiosk-Gerät noch nicht eingerichtet (Schlüssel nicht hinterlegt)."
    if code == 2 or "nicht erlaubt" in o:
        return "Das Kiosk-Gerät kennt diesen Befehl nicht."
    if code == 124:
        return "Das Kiosk-Gerät antwortet nicht (Zeitüberschreitung)."
    return (out or f"Fehler {code}")[:300]


async def phone_status(cached: bool = False) -> dict:
    if cached and time.time() - PHONE_CACHE["t"] < 20 and PHONE_CACHE["data"]:
        return PHONE_CACHE["data"]
    reach = bool(PHONE_HOST) and await asyncio.to_thread(tcp_open, PHONE_HOST, 22)
    data = {"reachable": reach, "host": PHONE_HOST, "configured": PHONE_KEY.exists(), "status": None, "error": None}
    if reach and PHONE_KEY.exists():
        code, out = await phone_cmd("status")
        if code == 0:
            try:
                data["status"] = json.loads(out)
            except ValueError:
                data["error"] = "Antwort des Kiosk-Geräts unlesbar."
        else:
            data["error"] = phone_error_text(out, code)
    PHONE_CACHE.update(t=time.time(), data=data)
    return data


@app.get("/api/phone")
async def phone_get():
    pub = BASE / "keys/handy_ed25519.pub"
    d = ({"reachable": False, "host": PHONE_HOST, "configured": PHONE_KEY.exists(), "status": None, "error": None}
         if KIOSK_PAUSED else await phone_status())
    d["paused"] = KIOSK_PAUSED
    d["pubkey"] = read_text(pub).strip() if pub.exists() else None
    return d


@app.post("/api/phone/{action}")
async def phone_action(action: str, request: Request):
    d = await body(request)
    if action == "reboot":
        if d.get("confirm") != "NEUSTART":
            raise HTTPException(400, "Bitte den Neustart bestätigen.")
        args = ["reboot"]
    elif action == "kiosk-reload":
        args = ["kiosk-reload"]
    elif action in ("helligkeit", "lautstaerke"):
        v = int(d.get("value", -1))
        if not 0 <= v <= 100:
            raise HTTPException(400, "Wert 0-100.")
        args = [action, str(v)]
    elif action == "bildschirm":
        if d.get("value") not in ("an", "aus"):
            raise HTTPException(400, "an oder aus.")
        args = ["bildschirm", d["value"]]
    else:
        raise HTTPException(404, "Unbekannte Aktion.")
    audit(who(request), f"Kiosk-Gerät: {' '.join(args)}")
    code, out = await phone_cmd(*args, timeout=12)
    PHONE_CACHE["t"] = 0
    if code != 0:
        raise HTTPException(502, f"Kiosk-Gerät: {phone_error_text(out, code)}")
    return {"ok": True, "output": out}


# ------------------------------------------------------------------ Prüfungen, Export
@app.post("/api/doctor")
async def doctor(request: Request):
    d = await body(request)
    which = d.get("which", "config")
    args = {"config": ("config", "check"), "doctor": ("doctor",), "status": ("status",),
            "skills": ("skills", "list"), "cron": ("cron", "status")}.get(which)
    if not args:
        raise HTTPException(400, "Unbekannte Prüfung.")
    code, out = await run_hermes(*args, timeout=150)
    return {"ok": code == 0, "output": out}


@app.get("/api/export")
async def export(request: Request):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for key in ("config", "soul", "memory", "user", "ui"):
            p, kind = FILES[key]
            if p.exists():
                text = read_text(p)
                if kind == "yaml":
                    text = "\n".join(redact_secret_yaml_lines(l + "\n").rstrip("\n") for l in text.splitlines()) + "\n"
                z.writestr(f"nuorb-export/{p.name}", text)
        envs = [f"{n}={'<geheim, nicht exportiert>' if env_is_secret(n) else v}" for n, v, _ in read_env() if n]
        z.writestr("nuorb-export/env-namen.txt", "\n".join(envs) + "\n")
        jobs = HERMES_HOME / "cron/jobs.json"
        if jobs.exists():
            z.writestr("nuorb-export/cron-jobs.json", read_text(jobs))
        for s in scan_skills():
            if s["own"]:
                z.writestr(f"nuorb-export/skills/{s['dir']}/SKILL.md",
                           read_text(HERMES_HOME / "skills" / s["dir"] / "SKILL.md"))
        z.writestr("nuorb-export/LIESMICH.txt",
                   "Export der NuOrb-Verwaltung vom " + now_iso() + ".\nSchlüssel/Passwörter sind NICHT enthalten.\n")
    audit(who(request), "Export heruntergeladen")
    fn = f"nuorb-export-{datetime.now():%Y%m%d-%H%M}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.get("/api/audit")
async def audit_log():
    return {"lines": tail(DATA / "audit.log", 200)}


# ------------------------------------------------------------------ Statische Dateien
STATIC = BASE / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/{name:path}")
async def static_file(name: str):
    p = (STATIC / name).resolve()
    if STATIC.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


UI_SHARED = {"tokens.css", "orb.js", "orb.css", "icon.svg"}


@app.get("/ui/{name:path}")
async def ui_shared(name: str):
    """Gemeinsame Design-Dateien direkt aus nuorb-ui (read-only, nichts kopiert)."""
    if name in UI_SHARED:
        p = UI_STATIC / name
    elif re.fullmatch(r"fonts/[a-z0-9\-]+\.woff2", name):
        p = UI_STATIC / name
    else:
        raise HTTPException(404)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=3600"})


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(UI_STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/nuorb-ca.crt")
async def ca():
    return FileResponse(UI_CERTS / "nuorb-ca.crt", media_type="application/x-x509-ca-cert")


if __name__ == "__main__":
    load_sessions()
    ensure_setup_code()
    ensure_internal_token()
    uvicorn.run(app, host=HOST, port=PORT, ssl_certfile=str(UI_CERTS / "server.crt"),
                ssl_keyfile=str(UI_CERTS / "server.key"), log_level="warning", proxy_headers=False,
                server_header=False)
