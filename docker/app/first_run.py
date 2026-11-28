#!/usr/bin/env python3
"""NuOrb-Server (Docker): Vorbereitung bei jedem Containerstart, Grundkonfiguration nur beim ersten Start.

Läuft als Benutzer hermes, bevor Oberfläche, Admin und Spracherkennung starten. Idempotent:
  * Ordner und Symlinks (Stimmen im Modell-Volume, Hermes-Cache zeigt darauf)
  * Zertifikate (eigene Heim-CA), API-Schlüssel zwischen Oberfläche und Hermes
  * nur beim ersten Start: Hermes-Grundeinstellungen (Bestätigungen, Sprache, keine Telemetrie/Update-Checks),
    LLM aus der .env (NUORB_INIT_*), ui.json mit Persona in der gewählten Sprache
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nuorb_common as jc  # noqa: E402
import personas  # noqa: E402

DATA = jc.DATA
MARK = jc.CONF / "first-run.json"
ENV = DATA / ".env"
VOICES = jc.MODELS / "voices"


def log(msg: str) -> None:
    print(f"[nuorb-init] {msg}", flush=True)


def hermes(*args: str) -> tuple[int, str]:
    p = subprocess.run(["hermes", *args], capture_output=True, text=True, timeout=180,
                       env={**os.environ, "HOME": str(DATA), "NO_COLOR": "1", "TERM": "dumb"})
    jc.reclaim_hermes_files()   # was die Hermes-CLI als nuorb geschrieben hat, sofort an hermes (wie im Panel)
    return p.returncode, (p.stdout + p.stderr)[-2000:]


def env_lines() -> list[str]:
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def env_get(name: str) -> str:
    for line in env_lines():
        if line.split("=", 1)[0].strip() == name:
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def env_set(values: dict[str, str]) -> None:
    lines, seen = [], set()
    for line in env_lines():
        k = line.split("=", 1)[0].strip()
        if k in values and not line.lstrip().startswith("#"):
            lines.append(f"{k}={values[k]}")
            seen.add(k)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    tmp = ENV.with_name(".env.tmp-nuorb")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)   # Gruppe hermes: dazu gehören nur hermes und nuorb (die Oberfläche liest API_SERVER_KEY)
    os.replace(tmp, ENV)


def dirs() -> None:
    for d in (jc.CONF, jc.CERTS, jc.J / "admin", jc.J / "ctl", VOICES, jc.MODELS / "stt", DATA / "cache"):
        d.mkdir(parents=True, exist_ok=True)
    # Getrennte Benutzer (v0.7.4): J 1770 und config 0750 wie in nuorb-init, sonst verliert Hermes (Policy-Hook,
    # Spracherkennung) den Zugang zu seinen Ordnern in J, bis nuorb-init sie zurücksetzt. Ohne Trennung wie bisher 0700.
    sep = jc.separated()
    for d, mode in ((jc.J, 0o1770 if sep else 0o700), (jc.CONF, 0o750 if sep else 0o700), (jc.CERTS, 0o700), (jc.J / "admin", 0o700)):
        os.chmod(d, mode)
    link = DATA / "cache/piper-voices"
    if link.is_symlink() or not link.exists():
        if link.is_symlink() and os.readlink(link) == str(VOICES):
            return
        link.unlink(missing_ok=True)
        link.symlink_to(VOICES)
    else:
        log(f"Note: {link} is a real folder, voices from {VOICES} are not shown there.")


def api_key() -> None:
    want = {"API_SERVER_ENABLED": "true", "API_SERVER_HOST": "127.0.0.1",
            "API_SERVER_PORT": jc.env("HERMES_API_PORT", "8642")}
    if not env_get("API_SERVER_KEY"):
        want["API_SERVER_KEY"] = secrets.token_hex(32)
    if any(env_get(k) != v for k, v in want.items()):
        env_set(want)
        log("Hermes API server enabled (127.0.0.1 inside the container only).")
        restart_gateways()
    k = jc.env("NUORB_INIT_ANTHROPIC_API_KEY")
    if k and not env_get("ANTHROPIC_API_KEY"):
        env_set({"ANTHROPIC_API_KEY": k})
        log("Anthropic key taken from .env (change it in the admin panel from now on).")


def restart_gateways() -> None:
    """Falls Hermes' Gateway schon vor dieser Vorbereitung lief, liest es die neue .env erst nach einem Neustart."""
    root = Path("/run/hermes-service")
    for slot in (sorted(root.glob("*")) if root.is_dir() else []):
        if (slot / "supervise").exists():
            subprocess.run(["s6-svc", "-r", str(slot)], check=False)
            log(f"Gateway {slot.name} restarted (new .env).")


def voice_name() -> str:
    v = jc.env("NUORB_VOICE") or jc.DEFAULT_VOICE["en" if jc.lang() == "en" else "de"]
    return jc.voice_replacement(v) or v


def first_config() -> None:
    lg = jc.lang()
    sets = [
        ("approvals.mode", "manual"),
        ("updates.check", "false"),
        ("telemetry.shared_metrics.enabled", "false"),
        ("telemetry.shared_metrics.send", "false"),
        ("stt.enabled", "true"),
        ("stt.provider", "openai"),
        ("stt.openai.base_url", f"http://127.0.0.1:{jc.env('STT_ROUTER_PORT', '8178')}/v1"),
        ("stt.openai.model", "parakeet"),
        ("stt.language", lg),
        ("stt.openai.language", lg),
        ("tts.provider", "piper"),
        ("tts.piper.voice", str(VOICES / f"{voice_name()}.onnx")),
    ]
    for k, v in sets:
        code, out = hermes("config", "set", k, v)
        if code != 0:
            log(f"hermes config set {k} failed: {out[-300:]}")
    # LLM aus der .env (optional; sonst Einrichtung im Admin-Panel)
    # Lokales Gehirn: Ollama (Standard) oder jeder OpenAI-kompatible Server (llama.cpp, vLLM, LM Studio, LocalAI)
    url = (jc.env("NUORB_INIT_LLM_URL") or jc.env("NUORB_INIT_OLLAMA_URL")).rstrip("/").removesuffix("/v1")
    model = jc.env("NUORB_INIT_LLM_MODEL") or jc.env("NUORB_INIT_OLLAMA_MODEL")
    ltype = "openai" if jc.env("NUORB_INIT_LLM_TYPE").lower() == "openai" else "ollama"
    lkey = jc.env("NUORB_INIT_LLM_API_KEY")
    ctx = int(jc.env("NUORB_INIT_CONTEXT", "65536") or 65536)
    os.environ.setdefault("NUORB_HOME", "/opt/nuorb/home")
    sys.path.insert(0, "/opt/nuorb/admin")
    import server as adm  # NuOrb-Admin: dieselbe Logik wie im Panel
    jc.admin_hooks(adm)
    path = adm.FILES["config"][0]
    if url and model:
        data = adm.load_config_rt()
        prov = getattr(adm, "OLLAMA_PROVIDER", "pc-ollama")
        server = "Ollama"
        if ltype == "openai":
            det = jc.llm_detect(url, lkey)
            server = det["server"]
            if det.get("ctx"):
                ctx = max(65536, int(det["ctx"]))
        adm.cfg_set(data, f"providers.{prov}.api", f"{url}/v1")
        adm.cfg_set(data, f"providers.{prov}.name", server)
        adm.cfg_set(data, f"providers.{prov}.models", {model: {"context_length": ctx}})
        if lkey:
            adm.cfg_set(data, f"providers.{prov}.api_key", lkey)
        for k, v in (("provider", f"custom:{prov}"), ("base_url", f"{url}/v1"), ("default", model), ("context_length", ctx)):
            adm.cfg_set(data, f"model.{k}", v)
        if ltype == "ollama":
            adm.cfg_set(data, "model.ollama_num_ctx", ctx)   # bei openai nicht: verdeckt sonst ein zu kleines -c am Server
        else:
            (data.get("model") or {}).pop("ollama_num_ctx", None)
        adm.atomic_write(path, adm.dump_rt(data))
        local = {"provider": f"custom:{prov}", "base_url": f"{url}/v1", "default": model, "type": ltype, "server": server}
        adm.save_brain_saved({"local": local})
        log(f"Local brain from .env: {server} ({ltype}) {url} · {model}")
    has_key = bool(env_get("ANTHROPIC_API_KEY"))
    mode = jc.env("NUORB_INIT_BRAIN") or ("auto" if (url and model and has_key) else "remote" if has_key else "local")
    ui = {"mode": "live", "persona": personas.PERSONA[lg], "piper_voice": voice_name(), "length_scale": 0.95,
          "features_zuhause": False, "features_geraet": False, "lang": lg}
    if (url and model) or has_key:
        try:
            ch = adm.build_brain_change(mode, jc.env("NUORB_INIT_CLAUDE_MODEL") or None, jc.env("NUORB_INIT_CLAUDE_MODEL") or None)
            adm.atomic_write(path, ch["text"])
            saved = ch["saved"]
            if url and model and isinstance(saved.get("local"), dict):
                saved["local"].update(type=ltype, server=server)   # Typ gehört NuOrb, nicht Hermes (§1.3)
            adm.save_brain_saved(saved)
            ui.update(ch["ui"])
            jc._write_json(jc.CONF / "setup-done.json", {"time": jc.now_iso(), "mode": mode, "via": ".env"})
            log(f"Brain from .env: {ch['desc']}")
        except Exception as exc:  # z. B. Modus ohne Schlüssel
            log(f"Brain setting from .env skipped: {getattr(exc, 'detail', exc)}")
    code, out = hermes("config", "check")
    log("hermes config check: " + ("ok" if code == 0 else out[-400:]))
    adm.atomic_write(jc.CONF / "ui.json", json.dumps(ui, indent=2, ensure_ascii=False) + "\n", 0o600)
    jc._write_json(MARK, {"time": jc.now_iso(), "lang": lg})
    log(f"Base configuration done (language {lg}, voice {voice_name()}).")


def ensure_mcp() -> None:
    """MCP-Server „nuorb“ (Name ändern, Nutzername, Kennenlernen) bei jedem Start sicherstellen."""
    os.environ.setdefault("NUORB_HOME", "/opt/nuorb/home")
    sys.path.insert(0, "/opt/nuorb/admin")
    import server as adm
    jc.admin_hooks(adm)
    data = adm.load_config_rt()
    want = {"command": "/opt/nuorb/venv/bin/python", "args": ["-I", "/opt/nuorb/app/nuorb_mcp.py"],
            "env": {"ADMIN_URL": os.environ.get("ADMIN_URL", "https://127.0.0.1:8444"),
                    "ADMIN_TOKEN_FILE": str(jc.CONF / "mcp.token")},   # enges Token, nur /internal/assistant
            "trust": "full", "timeout": 15, "tools": {"resources": False, "prompts": False}}
    adm.ensure_internal_token()   # legt auch mcp.token an, bevor Hermes den MCP-Server startet
    cur = adm.to_plain((data.get("mcp_servers") or {}).get("nuorb") or {})
    if {k: cur.get(k) for k in want} != want:
        adm.cfg_set(data, "mcp_servers.nuorb", want)
        adm.atomic_write(adm.FILES["config"][0], adm.dump_rt(data))
        log("MCP tool \"nuorb\" (name, onboarding) registered.")


def ensure_policy(existing: bool) -> None:
    """Werkzeuge von Hermes und Sicherheitsregeln (S5/S6) bei jedem Start sicherstellen.
    Enger Standard, solange im Admin-Panel nicht „Erweiterte Werkzeuge“ eingeschaltet ist. Eine Installation von vor
    v0.7.1 (keine tools.json) bekommt einen Hinweis im Admin-Panel, was abgeschaltet wurde."""
    os.environ.setdefault("NUORB_HOME", "/opt/nuorb/home")
    sys.path.insert(0, "/opt/nuorb/admin")
    import server as adm
    jc.admin_hooks(adm)
    if not jc.TOOLS_FILE.exists():
        jc._write_json(jc.TOOLS_FILE, {"mode": "narrow", "notice": existing, "since": jc.now_iso()})
        if existing:
            log("Hermes tools: narrow default (shell, files, browser off). Switch: admin panel, Tools.")
    mode = jc.tools_state()["mode"]
    data = adm.load_config_rt()
    if jc.apply_tools_policy(data, mode):
        adm.atomic_write(adm.FILES["config"][0], adm.dump_rt(data))
        log(f"Hermes tools and safety hooks set ({mode}).")
    jc.allow_policy_hooks()
    if jc.ensure_policy_rules():
        log("Tool rules file created (empty, built-in defaults apply). Admin panel: Files, Tool rules.")


SMARTHOME = Path("/opt/nuorb/smarthome")


def ensure_smarthome() -> None:
    """Smart Home (Home Assistant / ioBroker), optional: Skill + zwei MCP-Einträge aus einem Skript.
    smarthome (lesen, trust full) und smarthome_schalten (schalten, trust untrusted -> Bestätigungskarte).
    Standard aus. NUORB_SMARTHOME=1 schaltet ein, wenn die Zugangsdatei existiert; =0 schaltet aus;
    nicht gesetzt: Schalter bleibt, wie er in der Verwaltung steht. Zugang nur als Datei (600), nie im Image."""
    script, skill = SMARTHOME / "smarthome_mcp.py", SMARTHOME / "SKILL.md"
    if not script.is_file():
        return
    zugang = Path(os.environ.get("NUORB_SMARTHOME_ZUGANG") or DATA / "smarthome.env")   # dieselbe Datei wie in der Verwaltung
    dest = DATA / "skills/smart-home/smarthome/SKILL.md"
    if skill.is_file() and (not dest.is_file() or dest.read_bytes() != skill.read_bytes()):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill, dest)
    os.environ.setdefault("NUORB_HOME", "/opt/nuorb/home")
    sys.path.insert(0, "/opt/nuorb/admin")
    import server as adm
    jc.admin_hooks(adm)
    data = adm.load_config_rt()
    servers = adm.to_plain(data.get("mcp_servers") or {})
    schalter = os.environ.get("NUORB_SMARTHOME", "").strip()
    if schalter == "1" and not zugang.is_file():
        log(f"Smart home: NUORB_SMARTHOME=1, but the access file {zugang} is missing - staying off.")
    changed = False
    for name, teil, trust in (("smarthome", "lesen", "full"), ("smarthome_schalten", "schalten", "untrusted")):
        cur = servers.get(name) or {}
        enabled = bool(cur.get("enabled", False))
        if schalter == "1":
            enabled = zugang.is_file()
        elif schalter == "0":
            enabled = False
        want = {"command": "/opt/nuorb/venv/bin/python", "args": ["-I", str(script), teil, "--zugang", str(zugang)],
                "enabled": enabled, "trust": trust, "timeout": 25, "tools": {"resources": False, "prompts": False}}
        if teil == "schalten":
            want["supports_parallel_tool_calls"] = False
        if {k: cur.get(k) for k in want} != want:
            adm.cfg_set(data, f"mcp_servers.{name}", {**cur, **want})
            changed = True
    if changed:
        adm.atomic_write(adm.FILES["config"][0], adm.dump_rt(data))
        on = adm.to_plain(data.get("mcp_servers") or {}).get("smarthome", {}).get("enabled")
        log(f"Smart home MCP entries updated (smarthome, smarthome_schalten), {'on' if on else 'off'}.")
    if zugang.is_file() and zugang.stat().st_mode & 0o077:
        log(f"Smart home: {zugang} is readable by others - please chmod 600.")


WEATHER_SKILL = Path("/opt/nuorb/app/skills/wetter/SKILL.md")


def ensure_weather() -> None:
    """Eingebautes Wetter: Skill + MCP-Eintrag „wetter“ (nur lesend, trust full) bei jedem Start sicherstellen.
    Standard an (ohne eingerichteten Ort sagt das Werkzeug das ehrlich). Ein Schalter aus der Verwaltung
    (enabled) bleibt erhalten. Ort und Schlüssel liegen in CONF/weather.json bzw. weather.env (600)."""
    dest = DATA / "skills/nuorb/wetter/SKILL.md"
    if WEATHER_SKILL.is_file() and (not dest.is_file() or dest.read_bytes() != WEATHER_SKILL.read_bytes()):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WEATHER_SKILL, dest)
    os.environ.setdefault("NUORB_HOME", "/opt/nuorb/home")
    sys.path.insert(0, "/opt/nuorb/admin")
    import server as adm
    jc.admin_hooks(adm)
    data = adm.load_config_rt()
    cur = adm.to_plain((data.get("mcp_servers") or {}).get("wetter") or {})
    env = {"HERMES_HOME": str(DATA)}
    if os.environ.get("NUORB_WEATHER_MOCK", "").startswith("http://127.0.0.1:"):   # nur Tests (docker/tools/weather-e2e.py)
        env["NUORB_WEATHER_MOCK"] = os.environ["NUORB_WEATHER_MOCK"]
    want = {"command": "/opt/nuorb/venv/bin/python", "args": ["-I", "/opt/nuorb/app/nuorb_weather_mcp.py"],
            "env": env, "enabled": bool(cur.get("enabled", True)),
            "trust": "full", "timeout": 30, "tools": {"resources": False, "prompts": False}}
    if {k: cur.get(k) for k in want} != want:
        adm.cfg_set(data, "mcp_servers.wetter", {**cur, **want})
        adm.atomic_write(adm.FILES["config"][0], adm.dump_rt(data))
        log(f"MCP tool \"wetter\" (weather) registered, {'on' if want['enabled'] else 'off'}.")


def migrate_ollama_provider() -> None:
    """Bis v0.3.0 hieß der Ollama-Eintrag „lan-ollama“; die NuOrb-Verwaltung nutzt „pc-ollama“ (eine Quelle der Wahrheit).
    Einmalig umbenennen, in config.yaml und in den gemerkten Gehirn-Einstellungen. Eigene Einträge bleiben unberührt."""
    cfg = DATA / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return
    if "lan-ollama" not in text or re.search(r"^\s+pc-ollama:", text, re.M):
        return
    tmp = cfg.with_name(".config.yaml.migrate")
    tmp.write_text(text.replace("lan-ollama", "pc-ollama"), encoding="utf-8")
    os.chmod(tmp, cfg.stat().st_mode & 0o777)
    os.replace(tmp, cfg)
    for p in (jc.J / "admin").glob("brain*.json"):
        with contextlib.suppress(OSError):
            t = p.read_text(encoding="utf-8")
            if "lan-ollama" in t:
                p.write_text(t.replace("lan-ollama", "pc-ollama"), encoding="utf-8")
    log("Ollama entry renamed lan-ollama -> pc-ollama (same as Model & Fallback in the admin panel).")


def upgrade_persona() -> None:
    """Standard-Sätze älterer Versionen in der Persona (ui.json) auf den aktuellen Stand bringen (siehe personas.UPGRADES)."""
    f = jc.CONF / "ui.json"
    if not f.is_file():
        return
    ui = json.loads(f.read_text(encoding="utf-8"))
    old = ui.get("persona")
    if not isinstance(old, str):
        return
    new = personas.upgrade(old)
    if new != old:
        ui["persona"] = new
        tmp = f.with_name(".ui.json.persona")
        tmp.write_text(json.dumps(ui, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, f.stat().st_mode & 0o777)
        os.replace(tmp, f)
        log("Persona updated: internal system info is never read aloud or quoted.")


def migrate_voices() -> None:
    """v0.6.3: Stimmen ohne Freigabe für kommerzielle Nutzung (jc.RESTRICTED_VOICE) ersetzen – EN → Joe, DE → Thorsten –
    in ui.json und der Hermes-Konfiguration, und ihre Dateien aus dem Modell-Volume löschen."""
    f = jc.CONF / "ui.json"
    ui = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    if ui is not None:
        cur = ui.get("piper_voice") if isinstance(ui.get("piper_voice"), str) and ui.get("piper_voice") else jc.env("NUORB_VOICE")
        new = jc.voice_replacement(cur or "")
        if new:
            ui["piper_voice"] = new
            tmp = f.with_name(".ui.json.voice")
            tmp.write_text(json.dumps(ui, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(tmp, f.stat().st_mode & 0o777)
            os.replace(tmp, f)
            log(f"Voice {cur} is not licensed for commercial use - switched to {new}.")
            if jc.voice_replacement(jc.env("NUORB_VOICE")):
                log(f"Please remove NUORB_VOICE={jc.env('NUORB_VOICE')} from your .env (ignored from now on).")
    cfg = DATA / "config.yaml"
    with contextlib.suppress(OSError):
        m = re.search(r"piper-voices/([A-Za-z0-9_\-]+)\.onnx|/voices/([A-Za-z0-9_\-]+)\.onnx", cfg.read_text(encoding="utf-8"))
        old = m and (m.group(1) or m.group(2))
        if old and jc.voice_replacement(old):
            new = jc.voice_replacement(old)
            code, out = hermes("config", "set", "tts.piper.voice", str(VOICES / f"{new}.onnx"))
            log(f"Hermes voice {old} -> {new}" + ("" if code == 0 else f" failed: {out[-200:]}"))
    for p in sorted(VOICES.glob("*.onnx*")):
        name = p.name.split(".onnx")[0]
        if jc.voice_replacement(name):
            p.unlink(missing_ok=True)
            log(f"Removed voice file {p.name} (not licensed for commercial use).")


def main() -> None:
    dirs()
    jc.ensure_certs()
    api_key()
    try:
        migrate_ollama_provider()
    except Exception as exc:
        log(f"Ollama entry not migrated: {exc}")
    existing = MARK.exists()
    if not existing:
        first_config()
    try:
        migrate_voices()
    except Exception as exc:
        log(f"Voices not migrated: {exc}")
    try:
        upgrade_persona()
    except Exception as exc:
        log(f"Persona not updated: {exc}")
    try:
        ensure_mcp()
    except Exception as exc:
        log(f"MCP \"nuorb\" not registered: {exc}")
    try:
        ensure_smarthome()
    except Exception as exc:
        log(f"Smart home not set up: {exc}")
    try:
        ensure_weather()
    except Exception as exc:
        log(f"Weather not set up: {exc}")
    try:
        ensure_policy(existing)   # nach allen MCP-Einträgen
    except Exception as exc:
        log(f"Tool safety rules not set: {exc}")
    log(f"Ready. Home CA fingerprint: {jc.ca_fingerprint() or '(own certificate)'}")


if __name__ == "__main__":
    main()
