#!/usr/bin/env python3
"""NuOrb (Docker): API der Verwaltungsseite „Wetter“ (/static/docker/wetter.html).

Die Routen hängt admin_main.py an das Admin-Panel. Sie liegen damit hinter dessen Wächter: Heimnetz, Anmeldung und
CSRF-Schutz (POST nur mit Kopf X-NuOrb: 1). Alle Abfragen an Wetterdienste laufen hier auf dem Server, nie im Browser.
Schlüssel werden nur maskiert zurückgegeben und nie geloggt.

  GET  /api/weather                Einstellungen, ob Schlüssel gesetzt sind, Quellenhinweise
  GET  /api/weather/station        nächste DWD-Station mit Entfernung (Zwischenspeicher)
  POST /api/weather/search         {query}                     Ortssuche nur über den Ortsnamen (keine Postleitzahlen)
  POST /api/weather/place          {place}                     Ort übernehmen
  POST /api/weather/options        {provider, brightsky_url}
  POST /api/weather/keys           {openmeteo}                 optionaler Open-Meteo-Schlüssel ("" = löschen)
  POST /api/weather/test           {zeitraum, lang?}           Satz wie beim Vorlesen
  POST /api/weather/clear          {}                          Ort, Schlüssel und Zwischenspeicher löschen
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nuorb_common as jc  # noqa: E402
import nuorb_weather as nw  # noqa: E402

TR: Callable[[str, str], str] = lambda de, en: en   # noqa: E731  (admin_main setzt jc.tr: Sprache der Anfrage)
LANG: Callable[[], str] = lambda: "de"              # noqa: E731  (admin_main setzt jc.lang: Sprache des Assistenten)
KEY_RE = re.compile(r"[A-Za-z0-9._\-]{8,200}")
_last_search = [0.0]   # Ortssuche: höchstens eine Abfrage pro Sekunde (schont Open-Meteo)


def credits() -> list[dict]:
    """Quellenhinweise (Namensnennung) für die Seiten „Wetter“ und „Über“."""
    return [
        {"id": "dwd", "name": "Deutscher Wetterdienst", "notice": "Quelle: Deutscher Wetterdienst", "url": "https://www.dwd.de",
         "license": "CC BY 4.0", "license_url": "https://www.dwd.de/EN/service/legal_notice/legal_notice.html"},
        {"id": "brightsky", "name": "Bright Sky", "notice": "DWD data via Bright Sky", "url": "https://brightsky.dev",
         "license": "MIT", "license_url": "https://github.com/jdemaeyer/brightsky/blob/master/LICENSE"},
        {"id": "openmeteo", "name": "Open-Meteo", "notice": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com",
         "license": "CC BY 4.0", "license_url": "https://open-meteo.com/en/licence"},
        {"id": "geonames", "name": "GeoNames", "notice": "Location data based on GeoNames", "url": "https://www.geonames.org",
         "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/"},
    ]


async def _body(request: Request) -> dict:
    try:
        d = await request.json()
    except ValueError:
        raise HTTPException(400, TR("Ungültige Anfrage.", "Invalid request.")) from None
    if not isinstance(d, dict):
        raise HTTPException(400, TR("Ungültige Anfrage.", "Invalid request."))
    return d


def _mask(v: str | None) -> str:
    if not v:
        return ""
    return TR("gesetzt", "set") + (f" …{v[-4:]}" if len(v) >= 12 else "")


def state() -> dict:
    s = nw.load_settings()
    sec = nw.load_secrets()
    place = {k: s.get(k) for k in ("name", "region", "country_code", "lat", "lon", "timezone")} if s.get("lat") is not None else None
    return {"place": place, "provider": s.get("provider") or "auto", "effective": nw.provider_for(s) if place else None,
            "brightsky_url": s.get("brightsky_url") or "", "keys": {"openmeteo": _mask(sec.get("OPENMETEO_API_KEY"))},
            "credits": credits(), "periods": list(nw.PERIODS)}


def _err(e: nw.WeatherError) -> HTTPException:
    return HTTPException(502, TR(e.de, e.en))


async def weather_get(request: Request):
    return state()


async def weather_station(request: Request):
    return await asyncio.to_thread(nw.station_info)


async def weather_search(request: Request):
    d = await _body(request)
    q = str(d.get("query") or "").strip()[:80]
    if len(nw.place_name(q)) < 2:
        raise HTTPException(400, TR(nw.NUR_ZIFFERN.de, nw.NUR_ZIFFERN.en))
    wait = 1.1 - (time.monotonic() - _last_search[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_search[0] = time.monotonic()
    try:
        hits = await asyncio.to_thread(nw.geocode, q, LANG())
    except nw.WeatherError as e:
        raise _err(e)
    return {"results": hits}


def _place(p: dict) -> dict:
    try:
        lat, lon = float(p["lat"]), float(p["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, TR("Ort ohne gültige Koordinaten.", "Place without valid coordinates.")) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, TR("Ort ohne gültige Koordinaten.", "Place without valid coordinates."))
    cc = str(p.get("country_code") or "").upper()
    tz = str(p.get("timezone") or ("Europe/Berlin" if cc == "DE" else "UTC"))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = "UTC"
    return {"name": str(p.get("name") or "")[:80].strip() or TR("Mein Ort", "My place"),
            "region": str(p.get("region") or "")[:120],
            "country_code": cc if re.fullmatch(r"[A-Z]{2}", cc) else "", "lat": round(lat, 4), "lon": round(lon, 4), "timezone": tz}


async def weather_place(request: Request):
    d = await _body(request)
    s = nw.load_settings()
    s.update(_place(d.get("place") or {}))
    nw.save_settings(s)
    shutil.rmtree(nw.CACHE_DIR, ignore_errors=True)
    return state()


def _url(u: str) -> str:
    u = u.strip().rstrip("/")
    if not u:
        return ""
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname or p.query or p.fragment or p.username or p.password:
        raise HTTPException(400, TR("Ungültige Adresse für Bright Sky.", "Invalid Bright Sky address."))
    return u


async def weather_options(request: Request):
    d = await _body(request)
    s = nw.load_settings()
    if "provider" in d:
        if d["provider"] not in nw.PROVIDERS:
            raise HTTPException(400, TR("Unbekannter Anbieter.", "Unknown provider."))
        s["provider"] = d["provider"]
    if "brightsky_url" in d:
        u = _url(str(d.get("brightsky_url") or ""))
        if u and u != nw.BRIGHTSKY_URL:
            s["brightsky_url"] = u
        else:
            s.pop("brightsky_url", None)
    nw.save_settings(s)
    return state()


async def weather_keys(request: Request):
    d = await _body(request)
    if "openmeteo" in d:
        v = str(d.get("openmeteo") or "").strip()
        if v and not KEY_RE.fullmatch(v):
            raise HTTPException(400, TR("Der Schlüssel enthält ungültige Zeichen.", "The key contains invalid characters."))
        nw.save_secret("OPENMETEO_API_KEY", v or None)
    shutil.rmtree(nw.CACHE_DIR, ignore_errors=True)
    return state()


async def weather_test(request: Request):
    d = await _body(request)
    period = str(d.get("zeitraum") or "jetzt")
    lang = d.get("lang") if d.get("lang") in ("de", "en") else LANG()
    text, err = await asyncio.to_thread(nw.report, period if period in nw.PERIODS else "jetzt", lang)
    # Zwischenspeicher, den der Test als nuorb angelegt hat, an hermes (dessen Wetter-Werkzeug schreibt dort weiter)
    for p in (nw.CACHE_DIR, *nw.CACHE_DIR.glob("*")):
        jc.fix_owner(p)
    return {"text": text, "error": err}


async def weather_clear(request: Request):
    for p in (nw.SETTINGS_FILE, nw.SECRETS_FILE):
        p.unlink(missing_ok=True)
    shutil.rmtree(nw.CACHE_DIR, ignore_errors=True)
    return state()


def routes() -> list[APIRoute]:
    return [
        APIRoute("/api/weather", weather_get, methods=["GET"]),
        APIRoute("/api/weather/station", weather_station, methods=["GET"]),
        APIRoute("/api/weather/search", weather_search, methods=["POST"]),
        APIRoute("/api/weather/place", weather_place, methods=["POST"]),
        APIRoute("/api/weather/options", weather_options, methods=["POST"]),
        APIRoute("/api/weather/keys", weather_keys, methods=["POST"]),
        APIRoute("/api/weather/test", weather_test, methods=["POST"]),
        APIRoute("/api/weather/clear", weather_clear, methods=["POST"]),
    ]
