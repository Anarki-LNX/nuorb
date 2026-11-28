#!/usr/bin/env python3
"""NuOrb-Server (Docker): Start des Admin-Panels (NuOrb-Admin) im Container.

Lädt nuorb-admin/server.py unverändert als Modul und passt es an den Container an:
  * Dienste: s6 statt systemd (Status über s6-svstat, Neustart über s6-svc), Protokolle aus Dateien.
  * Kein Xiaomi-Handy (Seite „Handy“ ausgeblendet, kein SSH).
  * Neu: Ersteinrichtung (LLM wählen, Claude-Schlüssel) und „Gerät koppeln“ (QR-Code, Geräteliste).
Die neuen Seiten sind eigenständige HTML-Dateien unter /static/docker/, ihre API liegt hinter dem Admin-Login.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import base64
import io
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

sys.path.insert(0, "/opt/nuorb/admin")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nuorb_common as jc  # noqa: E402
import route_hooks  # noqa: E402
import update_check as uc  # noqa: E402
import server as adm  # noqa: E402  (nuorb-admin/server.py)
import weather_admin  # noqa: E402  (Verwaltungsseite „Wetter“)

weather_admin.TR, weather_admin.LANG = jc.tr, jc.lang

app = adm.app
# Getrennte Benutzer (v0.7.4): was das Panel als nuorb im Bereich von Hermes schreibt, geht an hermes zurück
jc.admin_hooks(adm)
PAGES = jc.APP / "pages"
SVC = {"hermes": None, "stt": "nuorb-stt", "ui": "nuorb-ui", "admin": "nuorb-admin"}
LOGS = jc.J / "logs"

# ------------------------------------------------------------------ Dienste über s6
adm.UNITS.clear()
# Sichtbare Kurznamen (die s6-Dienste heißen intern nuorb-*; die Zuordnung steht in SVC). Anzeigenamen: UNIT_LABELS von nuorb-admin.
adm.UNITS.update({"hermes": "gateway", "stt": "stt", "ui": "ui", "admin": "admin"})


def gateway_slot() -> Path | None:
    """Hermes registriert sein Gateway dynamisch unter /run/hermes-service/<name>."""
    root = Path("/run/hermes-service")
    with_sup = [p for p in sorted(root.glob("*")) if (p / "supervise").exists() and "gateway" in p.name] if root.is_dir() else []
    return with_sup[0] if with_sup else None


def svc_dir(name: str) -> Path | None:
    if name == "hermes":
        return gateway_slot()
    p = Path("/run/service") / str(SVC[name])
    return p if p.exists() else None


async def unit_state(name: str) -> dict:
    d = svc_dir(name)
    out = ""
    if d:
        p = await asyncio.create_subprocess_exec("s6-svstat", "-o", "up,pid,updownfor,normallyup", str(d),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out = (await p.communicate())[0].decode().strip()
    parts = out.split()
    up = parts[:1] == ["true"]
    since = None
    if len(parts) >= 3 and parts[2].isdigit():
        # ISO-Zeitstempel wie unter systemd; die Seite formatiert ihn je Sprache
        since = (dt.datetime.now().astimezone() - dt.timedelta(seconds=int(parts[2]))).isoformat(timespec="seconds")
    return {"id": name, "unit": adm.UNITS[name], "label": adm.UNIT_LABELS[name],
            "active": "active" if up else ("inactive" if d else "unknown"), "sub": "running" if up else "dead",
            "since": since, "memory": None, "restarts": None}


adm.unit_state = unit_state
adm.CTL_DIR = jc.J / "ctl"  # Verzeichnis existiert -> Admin-Seite bietet Neustarts an


async def service_restart(request: Request):
    name = request.path_params["name"]
    if name not in adm.UNITS:
        raise HTTPException(404, jc.tr("Unbekannter Dienst.", "Unknown service."))
    d = svc_dir(name)
    if not d:
        raise HTTPException(503, jc.tr("Dienst nicht gefunden.", "Service not found."))
    adm.audit(adm.who(request), f"Restart requested: {adm.UNITS[name]}")
    if name == "admin":  # sich selbst: Antwort zuerst, dann Neustart
        asyncio.get_running_loop().call_later(0.5, lambda: subprocess.Popen(["s6-svc", "-r", str(d)]))
        return {"ok": True, "self": True}
    p = await asyncio.create_subprocess_exec("s6-svc", "-r", str(d), stderr=asyncio.subprocess.PIPE)
    err = (await p.communicate())[1].decode()[:200]
    ok = p.returncode == 0
    res = {"id": "s6", "action": "restart", "unit": adm.UNITS[name], "ok": ok,
           "message": jc.tr("neu gestartet", "restarted") if ok else jc.tr("Neustart fehlgeschlagen", "restart failed") + f" {err}", "time": adm.now_iso()}
    adm.atomic_write(adm.CTL_DIR / "result.json", json.dumps(res), 0o600)
    return {"ok": ok, "result": res}


# Protokolle: Dateien statt journald
for key, fname, label in (("j-hermes", None, "Service: Hermes gateway"), ("j-stt", "stt", "Service: speech recognition"),
                          ("j-ui", "ui", "Service: UI"), ("j-admin", "admin", "Service: admin")):
    if fname:
        adm.LOG_SOURCES[key] = ("file", LOGS / fname / "current", label)
    else:
        adm.LOG_SOURCES.pop(key, None)

# ------------------------------------------------------------------ Aufrufe an die Oberfläche mit internem Token
_orig_probe = adm.probe


def _internal_headers() -> dict:
    try:
        return {"X-NuOrb-Internal": adm.INTERNAL_TOKEN_FILE.read_text().strip()}
    except OSError:
        return {}


async def probe(url: str, timeout: float = 1.5, headers: dict | None = None, verify=True):
    if url.startswith(adm.UI_URL):
        headers = {**(headers or {}), **_internal_headers()}
    if jc.tls_mode() == "custom" and url.startswith(adm.UI_URL):
        verify = False  # eigenes Zertifikat passt nicht auf 127.0.0.1; Aufruf bleibt im Container
    return await _orig_probe(url, timeout, headers, verify)


adm.probe = probe


async def tts_test(request: Request):
    d = await adm.body(request)
    text = str(d.get("text") or ("Guten Tag. Alle Systeme sind bereit." if jc.lang() == "de" else "Hello. All systems are ready."))[:300]
    try:
        async with httpx.AsyncClient(timeout=20, verify=adm.ui_ssl_ctx() if jc.tls_mode() == "selfsigned" else False) as c:
            r = await c.post(f"{adm.UI_URL}/api/tts", json={"text": text}, headers=_internal_headers())
        if r.status_code >= 400:
            raise HTTPException(502, jc.tr("Sprachausgabe nicht erreichbar.", "Speech output not reachable."))
        return Response(r.content, media_type=r.headers.get("content-type", "audio/wav"))
    except httpx.HTTPError:
        raise HTTPException(502, jc.tr("Sprachausgabe nicht erreichbar.", "Speech output not reachable."))


# ------------------------------------------------------------------ kein Xiaomi-Handy im generischen Image


async def phone_status(cached: bool = False) -> dict:
    return {"reachable": False, "host": "", "configured": False, "status": None, "error": None, "disabled": True}


adm.phone_status = phone_status


# ------------------------------------------------------------------ Smart Home: Skript liegt im Image, nicht unter ~/.hermes
if hasattr(adm, "SH_SCRIPT"):
    adm.SH_SCRIPT = Path("/opt/nuorb/smarthome/smarthome_mcp.py")


# ------------------------------------------------------------------ Einrichtungscode: Hinweis für Docker
_orig_auth_state = adm.auth_state


async def auth_state(request: Request):
    st = _orig_auth_state(request)
    st = dict(await st if asyncio.iscoroutine(st) else st)
    st["setupHint"] = None if adm.auth_configured() else \
        f"docker compose exec nuorb cat {adm.SETUP_FILE}\n# {jc.tr('oder', 'or')}: docker compose logs nuorb | grep -i 'setup code'"
    # Marke/Sprache für Login und Fußzeile (Feld „ui“ von NuOrb-Admin): Sprache nach ADMIN_LANG, Servername nur mit der Marke
    ui = dict(st.get("ui") or {})
    ui.update(brand=ui.get("brand") or jc.brand(), ui_lang=jc.ui_lang_setting(), server=jc.server_label())
    st["ui"] = ui
    st.setdefault("ui_lang", ui["ui_lang"])
    st.setdefault("brand", ui["brand"])
    return st


# ------------------------------------------------------------------ Ersteinrichtung (LLM + Schlüssel)
SETUP_DONE = jc.CONF / "setup-done.json"
OLLAMA_PROVIDER = getattr(adm, "OLLAMA_PROVIDER", "pc-ollama")  # derselbe Eintrag wie „Model & Fallback“ der NuOrb-Verwaltung


def setup_state() -> dict:
    cfg = adm.to_plain(adm.load_config_rt())
    model = cfg.get("model") or {}
    prov = (cfg.get("providers") or {}).get(OLLAMA_PROVIDER) or {}
    return {"done": SETUP_DONE.exists(), "lang": jc.lang(), "ui_lang": jc.ui_lang_setting(), "name": jc.server_name(),
            "ollama_url": (adm.ollama_base(cfg) if hasattr(adm, "ollama_base") else (prov.get("api") or "").removesuffix("/v1"))
            or jc.env("NUORB_INIT_OLLAMA_URL").removesuffix("/v1") or _bundled_ollama(),
            "ollama_model": (model.get("default") if str(model.get("provider", "")).startswith("custom:") else "")
            or jc.env("NUORB_INIT_OLLAMA_MODEL"),
            "anthropic_key": adm.mask(adm.env_value("ANTHROPIC_API_KEY")),
            "brain": adm.brain_state()["mode"], "claude_models": adm.CLAUDE_MODELS,
            "remote_model": adm.brain_state()["remote_model"],
            "voice": jc.voice_name(), "voice_choices": jc.VOICE_CHOICES,
            "voices_installed": sorted(p.stem for p in (jc.MODELS / "voices").glob("*.onnx")),
            "models": models_info(), **assistant_state(),
            # lokales Gehirn: Ollama oder OpenAI-kompatibel (docs/llm-backends.md), Werte von der NuOrb-Verwaltung
            "llm_type": adm.llm_type() if hasattr(adm, "llm_type") else "ollama",
            "llm_server": adm.llm_server(cfg) if hasattr(adm, "llm_server") else "Ollama",
            "llm_api_key_set": bool(adm.llm_key(cfg)) if hasattr(adm, "llm_key") else False}


def _bundled_ollama() -> str:
    """Vorschlag nur, wenn das Compose-Profil „ollama“ wirklich läuft (Dienstname auflösbar)."""
    import socket
    try:
        socket.getaddrinfo("ollama", 11434)
        return "http://ollama:11434"
    except OSError:
        return ""


def models_info() -> dict:
    st = jc.models_status()
    stt_ok = (jc.MODELS / "stt/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx").exists()
    return {**st, "stt_ready": stt_ok, "voice_ready": (jc.MODELS / "voices" / f"{jc.voice_name()}.onnx.json").exists()}


async def setup_models(request: Request):
    return models_info()


def about_info() -> dict:
    """Seite „Über“: Version, Spracherkennung und aktive Stimme mit Lizenz und Namensnennung."""
    voice = jc.voice_name()
    voices = [{"id": v, "lang": lg, "active": v == voice, **jc.VOICE_CREDITS.get(v, {"name": label})}
              for lg, opts in jc.VOICE_CHOICES.items() for v, label in opts]
    if voice not in {v["id"] for v in voices}:
        voices.insert(0, {"id": voice, "lang": voice[:2], "active": True, "name": voice, "license": "?"})
    return {"server": jc.server_label(), "brand": jc.brand(), "ui_lang": jc.ui_lang_setting(),
            "stt": {**jc.STT_CREDIT, "ready": models_info().get("stt_ready")},
            "voice": voice, "voices": voices, "weather": weather_admin.credits(),
            "licenses_url": "https://github.com/Anarki-LNX/nuorb/blob/main/THIRD_PARTY_LICENSES.md",
            "licenses_path": "/usr/share/doc/nuorb/"}


async def about_get(request: Request):
    return about_info()


PERSONA_DEFAULTS = None


async def setup_language(request: Request):
    """Sprache und Stimme: ui.json (lang, piper_voice, Persona falls unverändert), Hermes stt/tts; Stimme laden."""
    import personas
    d = await adm.body(request)
    lg = d.get("lang")
    voice = str(d.get("voice") or "")
    if lg not in ("de", "en"):
        raise HTTPException(400, jc.tr("Sprache de oder en.", "Language must be de or en."))
    if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}-[a-z0-9_]+-(x_low|low|medium|high)", voice):
        raise HTTPException(400, jc.tr("Ungültiger Stimmenname (z. B. de_DE-thorsten-medium).", "Invalid voice name (e.g. en_US-joe-medium)."))
    if jc.voice_replacement(voice):
        raise HTTPException(400, jc.tr(f"Die Stimme {voice} ist nicht für kommerzielle Nutzung freigegeben (Lizenz).",
                                       f"The voice {voice} is not licensed for commercial use."))
    ui = adm.read_ui_settings()
    fields = {"lang": lg, "piper_voice": voice}
    if not ui.get("persona") or ui.get("persona") in personas.PERSONA.values():
        fields["persona"] = personas.PERSONA[lg]   # eigene Persona bleibt unangetastet
    adm.write_ui_fields(fields, f"Einrichtung: Sprache {lg}, Stimme {voice}")
    for k, v in (("stt.language", lg), ("stt.openai.language", lg), ("tts.piper.voice", str(jc.MODELS / "voices" / f"{voice}.onnx"))):
        await adm.run_hermes("config", "set", k, v, timeout=60)
    adm.audit(adm.who(request), f"Einrichtung: Sprache {lg}, Stimme {voice}")
    if not (jc.MODELS / "voices" / f"{voice}.onnx.json").exists():
        subprocess.Popen(["/opt/nuorb/venv/bin/python", "-I", "/opt/nuorb/app/fetch_models.py", "voice", voice],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "downloading": voice}
    return {"ok": True}


async def setup_get(request: Request):
    return setup_state()


async def setup_ollama_probe(request: Request):
    """Verbindungstest wie in der Verwaltung (llm_detect, Vertrag §1.4): Ollama oder OpenAI-kompatibel."""
    d = await adm.body(request)
    typ = d.get("type") if d.get("type") in ("ollama", "openai") else "ollama"
    url, warn = _normalize_llm(str(d.get("url", "")), typ)
    key = str(d.get("api_key") or "").strip() or (adm.llm_key() if hasattr(adm, "llm_key") else "")
    if hasattr(adm, "llm_detect"):
        try:   # prefer_ip: Adresse, unter der der Browser NuOrb aufruft (Fallback, wenn NUORB_HOST_IP fehlt)
            res = await adm.llm_detect(typ, url, key, prefer_ip=request.url.hostname or "")
        except TypeError:   # ältere Verwaltung ohne prefer_ip
            res = await adm.llm_detect(typ, url, key)
        if not res.get("ok"):
            return JSONResponse({"detail": res.get("error") or jc.tr("Server nicht erreichbar", "Server not reachable"),
                                 "firewall": res.get("firewall")}, status_code=502)
        return {"ok": True, "url": res.get("base") or url, "type": typ, "server": res.get("server"), "version": res.get("version"),
                "models": res.get("models") or [], "ctx": res.get("ctx"), "ctx_short": res.get("ctx_short") or [],
                "warn": res.get("warn") or warn}
    try:   # ältere Verwaltung ohne llm_detect: nur Ollama
        async with httpx.AsyncClient(timeout=5) as c:
            v = (await c.get(f"{url}/api/version")).json()
            tags = (await c.get(f"{url}/api/tags")).json()
    except Exception as exc:
        raise HTTPException(502, jc.tr(f"Ollama unter {url} nicht erreichbar", f"Ollama at {url} not reachable") + f" ({exc.__class__.__name__}).")
    return {"ok": True, "url": url, "type": "ollama", "server": "Ollama", "warn": warn, "version": v.get("version"),
            "models": sorted(m.get("name") for m in tags.get("models", []))}


def _normalize_llm(raw: str, typ: str = "ollama") -> tuple[str, str]:
    """Adresse wie in der Verwaltung prüfen (llm_normalize): ollama mit Port 11434, openai ohne geratenen Port."""
    if hasattr(adm, "llm_normalize"):
        return adm.llm_normalize(raw, typ)
    return _normalize_ollama(raw)


def _normalize_ollama(raw: str) -> tuple[str, str]:
    """Gleiche Prüfung wie die NuOrb-Verwaltung (ollama_normalize): Schema/Port ergänzen, /v1 weg, Warnung bei localhost."""
    if hasattr(adm, "ollama_normalize"):
        return adm.ollama_normalize(raw)
    url = raw.strip().rstrip("/").removesuffix("/v1")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]{1,200}(:\d{1,5})?", url):
        raise HTTPException(400, jc.tr("Ungültige Adresse (z. B. http://192.0.2.30:11434).", "Invalid address (e.g. http://192.0.2.30:11434)."))
    return url, ""


def _env_set(name: str, value: str | None) -> None:
    lines, found = [], False
    for n, _v, raw in adm.read_env():
        if n == name:
            found = True
            if value:
                lines.append(f"{name}={value}")
        else:
            lines.append(raw)
    if not found and value:
        lines.append(f"{name}={value}")
    path = adm.FILES["env"][0]
    adm.backup("env", adm.read_text(path), f"Setup: {name}")
    adm.atomic_write(path, "\n".join(lines) + "\n", 0o600)


async def setup_apply(request: Request):
    """Schreibt LLM-Wahl: Ollama-Provider, Claude-Schlüssel, Gehirn-Modus. Danach Gateway-Neustart."""
    d = await adm.body(request)
    mode = d.get("mode")
    if mode not in ("local", "auto", "remote"):
        raise HTTPException(400, jc.tr("Modus fehlt (local, auto oder remote).", "Mode missing (local, auto or remote)."))
    typ = d.get("llm_type") if d.get("llm_type") in ("ollama", "openai") else "ollama"
    url = _normalize_llm(str(d.get("ollama_url")), typ)[0] if str(d.get("ollama_url") or "").strip() else ""
    model = str(d.get("ollama_model") or "").strip()
    lkey = str(d.get("llm_api_key") or "").strip()
    lkey_clear = bool(d.get("llm_api_key_clear"))
    key = str(d.get("anthropic_key") or "").strip()
    ctx = int(d.get("context") or 65536)
    if mode in ("local", "auto") and not (url and re.fullmatch(r"[A-Za-z0-9._:/\-]{1,120}", model)):
        raise HTTPException(400, jc.tr("Für Lokal/Automatisch braucht es die Adresse des lokalen Servers und ein Modell.",
                                       "Local/automatic needs the address of the local server and a model."))
    if key and not re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]{20,300}", key):
        raise HTTPException(400, jc.tr("Das sieht nicht wie ein Anthropic-Schlüssel aus (sk-ant-…).", "That does not look like an Anthropic key (sk-ant-…)."))
    if not 64000 <= ctx <= 1048576:
        raise HTTPException(400, jc.tr("Hermes braucht mindestens 64k Kontext (64000 bis 1048576).", "Hermes needs at least 64k context (64000 to 1048576)."))
    who = adm.who(request)
    if key:
        _env_set("ANTHROPIC_API_KEY", key)
        adm.audit(who, "Setup: Anthropic key set")
    if mode != "local" and not adm.env_value("ANTHROPIC_API_KEY"):
        raise HTTPException(400, jc.tr("Für Claude fehlt der Anthropic-Schlüssel.", "Claude needs an Anthropic key."))
    path = adm.FILES["config"][0]
    old = adm.read_text(path)
    data = adm.load_config_rt()
    server = "Ollama"
    if url:
        # Schlüssel des lokalen Servers: neu gesetzt, gelöscht oder der gespeicherte bleibt (nie in Logs/Audit)
        cur = adm.to_plain(data)
        key_eff = "" if lkey_clear else (lkey or (adm.llm_key(cur) if hasattr(adm, "llm_key") else ""))
        if typ == "openai" or key_eff:
            det = await adm.llm_detect(typ, url, key_eff) if hasattr(adm, "llm_detect") else {}
            if det.get("ok"):
                server = det.get("server") or server
                if det.get("ctx"):
                    ctx = max(ctx, int(det["ctx"]))
            elif typ == "openai":
                server = "OpenAI-kompatibel"
        prov = (cur.get("providers") or {}).get(OLLAMA_PROVIDER) or {}
        adm.cfg_set(data, f"providers.{OLLAMA_PROVIDER}.api", f"{url}/v1")
        # Anzeigename: erkannter Server; bei Ollama bleibt ein eigener Name stehen (wie in der Verwaltung)
        if typ == "openai" or not prov.get("name"):
            adm.cfg_set(data, f"providers.{OLLAMA_PROVIDER}.name", server)
        adm.cfg_set(data, f"providers.{OLLAMA_PROVIDER}.models", {model: {"context_length": ctx}} if model else {})
        if key_eff:
            adm.cfg_set(data, f"providers.{OLLAMA_PROVIDER}.api_key", key_eff)
        else:
            ((data.get("providers") or {}).get(OLLAMA_PROVIDER) or {}).pop("api_key", None)
        adm.cfg_set(data, "model.provider", f"custom:{OLLAMA_PROVIDER}")
        adm.cfg_set(data, "model.base_url", f"{url}/v1")
        adm.cfg_set(data, "model.default", model)
        adm.cfg_set(data, "model.context_length", ctx)
        if typ == "ollama":
            adm.cfg_set(data, "model.ollama_num_ctx", ctx)
        else:   # bei openai verdeckt ollama_num_ctx sonst ein zu kleines Fenster am Server (Vertrag §1.3)
            (data.get("model") or {}).pop("ollama_num_ctx", None)
        saved = adm.load_brain_saved()
        saved["local"] = {"provider": f"custom:{OLLAMA_PROVIDER}", "base_url": f"{url}/v1", "default": model,
                          "type": typ, "server": server}
        adm.save_brain_saved(saved)
    adm.backup("config", old, "Setup: LLM")
    adm.atomic_write(path, adm.dump_rt(data))
    # Gehirn-Modus mit derselben Logik wie Admin-Seite und Handy-Schalter
    remote = d.get("remote_model") if d.get("remote_model") in adm.CLAUDE_MODELS else None
    ch = adm.build_brain_change(mode, remote, remote)
    adm.atomic_write(path, ch["text"])
    code, out = await adm.run_hermes("config", "check", timeout=90)
    if code != 0:
        adm.atomic_write(path, old)
        raise HTTPException(500, jc.tr("Hermes lehnt die Konfiguration ab, alte Einstellung wiederhergestellt: ", "Hermes rejected the configuration, previous settings restored: ") + out[-400:])
    saved = ch["saved"]
    if url and isinstance(saved.get("local"), dict):
        saved["local"].update(type=typ, server=server)   # Typ gehört NuOrb, nicht Hermes (Vertrag §1.3)
    adm.save_brain_saved(saved)
    adm.write_ui_fields(ch["ui"], "Setup: brain mode")
    adm.audit(who, f"Setup: {ch['desc']}")
    jc._write_json(SETUP_DONE, {"time": adm.now_iso(), "mode": mode})
    gw = gateway_slot()
    if gw:  # Schlüssel aus .env liest Hermes beim Start
        await (await asyncio.create_subprocess_exec("s6-svc", "-r", str(gw))).wait()
    return {"ok": True, "desc": ch["desc"], "privacy": ch["privacy"]}


# ------------------------------------------------------------------ Gerät koppeln
def public_url(request: Request) -> str:
    """Adresse der Oberfläche, wie Geräte sie erreichen: NUORB_PUBLIC_URL, sonst Host aus dem Browser-Aufruf."""
    pub = jc.env("NUORB_PUBLIC_URL")
    if pub:
        return pub.rstrip("/")
    host = request.headers.get("host", "localhost").rsplit(":", 1)[0] if not request.headers.get("host", "").startswith("[") \
        else request.headers.get("host", "").rsplit("]:", 1)[0] + "]"
    scheme = "http" if jc.tls_mode() == "off" else "https"  # off ohne NUORB_PUBLIC_URL: nur zum lokalen Testen
    return f"{scheme}://{host}:{jc.env('NUORB_UI_PORT_PUBLIC', jc.env('UI_PORT', '8443'))}"


def qr_svg_data(text: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


async def pairing_new(request: Request):
    code = jc.new_pairing_code(adm.who(request))
    url = public_url(request)
    fp = jc.ca_fingerprint()
    q = {"v": "1", "url": url, "code": code["code"], "name": jc.server_name()}
    if fp:
        q["fp"] = fp
    uri = "nuorb://pair?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    browser = f"{url}/pair?code={code['code']}"
    host = urllib.parse.urlparse(url).hostname or ""
    adm.audit(adm.who(request), "Pairing code created")
    return {**code, "url": url, "fingerprint": fp, "uri": uri, "browser_url": browser,
            "qr": qr_svg_data(uri), "qr_browser": qr_svg_data(browser),
            "host_in_cert": jc.tls_mode() != "selfsigned" or host in jc.cert_names(), "cert_names": jc.cert_names()}


async def pairing_devices(request: Request):
    out = [{"id": k, **{x: v.get(x) for x in ("name", "platform", "created", "last_seen", "ip")}}
           for k, v in jc.devices().items()]
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return {"devices": out, "pairing_required": jc.pairing_required(), "fingerprint": jc.ca_fingerprint(),
            "tls": jc.tls_mode()}


async def pairing_revoke(request: Request):
    d = await adm.body(request)
    did = str(d.get("id", ""))
    if not jc.revoke_device(did):
        raise HTTPException(404, jc.tr("Gerät nicht gefunden.", "Device not found."))
    adm.audit(adm.who(request), f"Device unpaired: {did}")
    return {"ok": True}


# ------------------------------------------------------------------ Name des Assistenten, Nutzername, Kennenlernen
NAME_RE = re.compile(r"[\w .'\-]{1,40}")
USER_MEM_PREFIX = ("Name des Nutzers:", "User's name:")


def _remember_user(name: str) -> None:
    path = adm.FILES["user"][0]
    old = adm.read_text(path)
    parts = [x for x in old.split(adm.MEM_SEP) if x.strip() and not x.strip().startswith(USER_MEM_PREFIX)]
    parts.append(f"{USER_MEM_PREFIX[0 if jc.lang() == 'de' else 1]} {name}")
    adm.backup("user", old, "User name remembered")
    adm.atomic_write(path, adm.MEM_SEP.join(p.strip() for p in parts) + "\n")


def assistant_state() -> dict:
    import personas
    return {"assistant_name": jc.assistant_name(), "brand": jc.brand(), "user_name": jc.user_name(),
            "onboarded": jc.onboarded(), "greeting": personas.GREETING[jc.lang()].replace("{brand}", jc.brand())}


def assistant_update(d: dict, who: str) -> dict:
    fields: dict[str, str | bool | None] = {}
    notes: list[str] = []
    for key in ("assistant_name", "user_name"):
        if key in d and d[key] is not None:
            v = re.sub(r"\s+", " ", str(d[key])).strip()
            if v and not NAME_RE.fullmatch(v):
                raise HTTPException(400, jc.tr("Name: 1-40 Zeichen, Buchstaben, Ziffern, Leerzeichen, . ' -", "Name: 1-40 characters, letters, digits, spaces, . ' -"))
            fields[key] = v or None   # leer = zurück auf Standard
            notes.append(f"{key}={v or '(default)'}")
    if "onboarded" in d:
        if not isinstance(d["onboarded"], bool):
            raise HTTPException(400, "onboarded must be true/false.")
        fields["onboarded"] = d["onboarded"]
        notes.append(f"onboarded={d['onboarded']}")
    if not fields:
        raise HTTPException(400, jc.tr("Nichts zu ändern.", "Nothing to change."))
    adm.write_ui_fields(fields, "Name/onboarding")
    if fields.get("user_name"):
        _remember_user(str(fields["user_name"]))
    adm.audit(who, "Assistant: " + ", ".join(notes))
    return {"ok": True, **assistant_state()}


async def internal_assistant(request: Request):
    if request.method == "GET":
        return assistant_state()
    d = await adm.body(request)
    src = re.sub(r"[^a-z0-9\-]", "", str(d.pop("source", "intern")))[:20] or "intern"
    return assistant_update(d, f"internal:{src}")


async def api_assistant(request: Request):
    if request.method == "GET":
        return assistant_state()
    return assistant_update(await adm.body(request), adm.who(request))


# ------------------------------------------------------------------ Werkzeuge von Hermes (S5/S6)
# Regeldatei im Datei-Editor (Vorschau, Sicherung, Rückgängig wie alle Änderungen), Entscheidungen bei den Protokollen
adm.FILES["policy"] = (jc.POLICY_RULES, "json")
adm.FILE_LABELS["policy"] = "Werkzeug-Regeln (rules.json)"
adm.LOG_SOURCES["policy"] = ("file", jc.POLICY_LOG, "Werkzeuge: Entscheidungen der Regeln")


def policy_overview(limit: int = 40) -> dict:
    """Wirksame Regeln für die Seite Werkzeuge und die letzten Entscheidungen (neueste zuerst)."""
    sys.path.insert(0, str(jc.APP))
    import nuorb_policy as pol
    rules, problem = pol.load_rules()
    log: list[dict] = []
    try:
        lines = jc.POLICY_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
        for line in reversed(lines):
            try:
                log.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    defaults = [{"match": "(tools with an effect)", "level": "confirm_after_outside", "builtin": True},
                {"match": "*", "level": "allow", "builtin": True}]
    try:
        text = jc.POLICY_RULES.read_text(encoding="utf-8")
    except OSError:
        text = json.dumps(jc.POLICY_TEMPLATE, indent=2, ensure_ascii=False) + "\n"
    return {"rules": rules, "defaults": defaults, "problem": problem, "log": log, "text": text}


async def tools_rules(request: Request):
    """Neue rules.json prüfen und als Vorschau anlegen; Übernehmen über /api/change/{id}/apply (Sicherung, Rückgängig)."""
    sys.path.insert(0, str(jc.APP))
    import nuorb_policy as pol
    d = await adm.body(request)
    text = str(d.get("text") or "")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise HTTPException(400, jc.tr(f"Kein gültiges JSON: {exc}", f"Not valid JSON: {exc}")) from None
    _, errors = pol.parse_rules(raw)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    jc.ensure_policy_rules()
    return adm.make_preview("policy", text if text.endswith("\n") else text + "\n",
                            jc.tr("Werkzeug-Regeln ändern", "Change tool rules"),
                            jc.tr("Gilt ab dem nächsten Werkzeugaufruf, ohne Neustart.", "Applies from the next tool call, no restart."))


async def tools_api(request: Request):
    """Enger Standard oder erweiterte Werkzeuge. Schreibt platform_toolsets und die Policy-Hooks, startet Hermes neu."""
    st = jc.tools_state()
    if request.method == "GET":
        return {**st, "toolsets": jc.TOOLSETS[st["mode"]], "brand": jc.brand(), "ui_lang": jc.ui_lang_setting(),
                "policy": policy_overview()}
    d = await adm.body(request)
    if set(d) - {"mode", "notice_seen"} or ("mode" in d and d["mode"] not in jc.TOOLSETS):
        raise HTTPException(400, jc.tr("Erlaubt: mode (narrow oder extended), notice_seen.", "Allowed: mode (narrow or extended), notice_seen."))
    new = {**st, "notice": False if d.get("notice_seen") else st["notice"]}
    restart = False
    if "mode" in d and d["mode"] != st["mode"]:
        new["mode"] = d["mode"]
        data = adm.load_config_rt()
        if jc.apply_tools_policy(data, new["mode"]):
            adm.atomic_write(adm.FILES["config"][0], adm.dump_rt(data))
            restart = True
        jc.allow_policy_hooks()
        adm.audit(adm.who(request), f"Hermes tools: {st['mode']} -> {new['mode']}")
    jc._write_json(jc.TOOLS_FILE, {**new, "changed": jc.now_iso()})
    if restart and (slot := svc_dir("hermes")):
        p = await asyncio.create_subprocess_exec("s6-svc", "-r", str(slot), stderr=asyncio.subprocess.PIPE)
        await p.communicate()
    return {**new, "toolsets": jc.TOOLSETS[new["mode"]], "restarted": restart}


# ------------------------------------------------------------------ Update-Prüfung (v0.7.3, Logik in update_check.py)
async def update_api(request: Request):
    """GET: Zustand (bei Bedarf Prüfung im Hintergrund). POST {"check": true}: sofort prüfen. POST {"auto": bool}."""
    if request.method == "GET":
        st = uc.state()
        if uc.due():
            asyncio.get_running_loop().create_task(uc.fetch())
        return st
    d = await adm.body(request)
    if set(d) - {"check", "auto", "update"} or ("auto" in d and not isinstance(d["auto"], bool)):
        raise HTTPException(400, jc.tr("Erlaubt: check, auto (true/false), update.", "Allowed: check, auto (true/false), update."))
    if "update" in d:
        try:
            st = uc.request_update(str(d["update"]))
        except PermissionError:
            raise HTTPException(409, jc.tr("Keine Update-Hilfe installiert. Bitte den angezeigten Befehl ausführen.",
                                           "No update helper installed. Please run the command shown.")) from None
        except ValueError:
            raise HTTPException(400, jc.tr("Nur die gefundene neuere Version kann installiert werden.",
                                           "Only the newer version that was found can be installed.")) from None
        except RuntimeError:
            raise HTTPException(429, jc.tr("Ein Update läuft schon oder wurde gerade angefordert (höchstens alle 10 Minuten).",
                                           "An update is running or was just requested (at most every 10 minutes).")) from None
        adm.audit(adm.who(request), f"Update to {d['update']} requested")
        return st
    if "auto" in d:
        uc.set_auto(d["auto"])
        adm.audit(adm.who(request), f"Automatic update check {'on' if d['auto'] else 'off'}")
    if d.get("check"):
        if not uc.enabled():
            raise HTTPException(403, jc.tr("Update-Prüfung ist mit NUORB_UPDATE_CHECK=0 abgeschaltet.",
                                           "The update check is switched off with NUORB_UPDATE_CHECK=0."))
        await uc.fetch()
    return uc.state()


# ------------------------------------------------------------------ Seiten und Zusatz-Skript
async def docker_page(request: Request):
    name = request.path_params["name"]
    if not re.fullmatch(r"[a-z\-]+\.(html|js|css)", name) or not (PAGES / name).is_file():
        raise HTTPException(404)
    return FileResponse(PAGES / name, headers={"Cache-Control": "no-cache"})


async def index_plus(request: Request):
    html = (adm.STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("</body>", '<script src="/static/docker/admin-extra.js"></script>\n</body>', 1)
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


NEW = [
    APIRoute("/api/auth/state", auth_state, methods=["GET"]),
    APIRoute("/api/services/{name}/restart", service_restart, methods=["POST"]),
    APIRoute("/api/tts-test", tts_test, methods=["POST"]),
    APIRoute("/api/setup", setup_get, methods=["GET"]),
    APIRoute("/internal/assistant", internal_assistant, methods=["GET", "POST"]),
    APIRoute("/api/assistant", api_assistant, methods=["GET", "POST"]),
    APIRoute("/api/setup/ollama", setup_ollama_probe, methods=["POST"]),
    APIRoute("/api/setup/apply", setup_apply, methods=["POST"]),
    APIRoute("/api/setup/language", setup_language, methods=["POST"]),
    APIRoute("/api/setup/models", setup_models, methods=["GET"]),
    APIRoute("/api/about", about_get, methods=["GET"]),
    APIRoute("/api/pairing/new", pairing_new, methods=["POST"]),
    APIRoute("/api/pairing/devices", pairing_devices, methods=["GET"]),
    APIRoute("/api/pairing/revoke", pairing_revoke, methods=["POST"]),
    *weather_admin.routes(),
    APIRoute("/api/update", update_api, methods=["GET", "POST"]),
    APIRoute("/api/tools", tools_api, methods=["GET", "POST"]),
    APIRoute("/api/tools/rules", tools_rules, methods=["POST"]),
    APIRoute("/static/docker/{name}", docker_page, methods=["GET"]),
    APIRoute("/", index_plus, methods=["GET"]),
]
for _r in route_hooks.extend_routes(app, NEW):  # vor die Originale, verdeckte Originale raus
    print(f"[nuorb-admin] Route ersetzt: {_r}", flush=True)


if __name__ == "__main__":
    import uvicorn
    jc.ensure_certs()
    adm.CTL_DIR.mkdir(parents=True, exist_ok=True)
    adm.load_sessions()
    adm.ensure_setup_code()
    if not adm.auth_configured():
        try:
            code = adm.SETUP_FILE.read_text().strip()
            print(f"[nuorb-admin] ===== setup code / Einrichtungscode: {code} =====", flush=True)
        except OSError:
            pass
    adm.ensure_internal_token()
    async def lang_app(scope, receive, send):
        """Sprache der Anfrage (Kopf X-UI-Lang) für jc.tr(); jede Anfrage läuft in eigenem Task."""
        if scope["type"] == "http":
            h = dict(scope.get("headers") or []).get(b"x-ui-lang", b"").decode("latin-1")[:2].lower()
            jc.REQ_LANG.set(h if h in ("de", "en") else None)
        await app(scope, receive, send)

    uvicorn.run(lang_app, host=jc.env("ADMIN_HOST", "0.0.0.0"), port=int(jc.env("ADMIN_PORT", "8444")),
                log_level="warning", server_header=False, **jc.proxy_kwargs(), **jc.ssl_kwargs())
