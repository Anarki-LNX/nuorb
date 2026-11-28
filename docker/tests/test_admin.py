"""Unit-Tests für das Admin-Panel (docker/src/admin/server.py): Einrichtung, Anmeldung, Sperre, CSRF, Cookies,
Vorschau mit Sicherung und Rückgängig, Pfadprüfungen, Schwärzung."""
from __future__ import annotations

import json
import stat
import unittest

from fastapi.testclient import TestClient

from _lade import SRC, lade, temp_ordner

T = temp_ordner("admin")
adm = lade("nuorb_admin_test", SRC / "admin/server.py", {
    "NUORB_HOME": str(T), "ADMIN_DATA": str(T / "data"), "HERMES_HOME": str(T / "hermes"),
    "HERMES_BIN": str(T / "fehlt/hermes"), "NUORB_UI_SETTINGS": str(T / "ui.json"), "ADMIN_CTL_DIR": str(T / "ctl"),
    "ADMIN_CERTS": str(T / "certs"), "NUORB_UI_STATIC": str(T / "static"),
    "NUORB_ALLOWED_HOSTS": "nuorb.example.org,*.ts.net",
})
BASE = "https://192.168.1.50:8444"
PW = "Test-Passwort-sicher-42"


def client(ip: str = "192.168.1.60", **h) -> TestClient:
    c = TestClient(adm.app, base_url=BASE, client=(ip, 50000))
    c.headers.update({"X-NuOrb": "1", "Origin": BASE, **h})
    return c


def einrichten() -> None:
    if not adm.auth_configured():
        adm.ensure_setup_code()
        code = adm.SETUP_FILE.read_text().strip()
        r = client("192.168.1.61").post("/api/auth/setup", json={"code": code, "password": PW})
        assert r.status_code == 200, r.text


def angemeldet(ip: str = "192.168.1.62") -> TestClient:
    einrichten()
    c = client(ip)
    r = c.post("/api/auth/login", json={"password": PW})
    assert r.status_code == 200, r.text
    return c


class A0Einrichtung(unittest.TestCase):  # vor allen anderen Klassen (unittest sortiert nach Namen)
    def test_1_setup_code_nur_fuer_den_besitzer_lesbar(self):
        if adm.auth_configured():
            self.skipTest("schon eingerichtet")
        adm.ensure_setup_code()
        self.assertEqual(stat.S_IMODE(adm.SETUP_FILE.stat().st_mode), 0o600)

    def test_2_falscher_code_wird_abgelehnt(self):
        if adm.auth_configured():
            self.skipTest("schon eingerichtet")
        adm.ensure_setup_code()
        r = client("192.168.1.70").post("/api/auth/setup", json={"code": "AAAA-BBBB-CCCC", "password": PW})
        self.assertEqual(r.status_code, 403)

    def test_3_richtiger_code_setzt_passwort_und_loescht_code(self):
        einrichten()
        self.assertTrue(adm.auth_configured())
        self.assertFalse(adm.SETUP_FILE.exists())
        self.assertEqual(stat.S_IMODE(adm.AUTH_FILE.stat().st_mode), 0o600)
        self.assertEqual(json.loads(adm.AUTH_FILE.read_text())["alg"], "scrypt")
        self.assertNotIn(PW, adm.AUTH_FILE.read_text())

    def test_4_zweite_einrichtung_geht_nicht(self):
        einrichten()
        r = client("192.168.1.71").post("/api/auth/setup", json={"code": "egal", "password": PW})
        self.assertEqual(r.status_code, 409)


class Anmeldung(unittest.TestCase):
    def test_falsches_passwort(self):
        einrichten()
        self.assertEqual(client("192.168.1.80").post("/api/auth/login", json={"password": "falsch-falsch"}).status_code, 403)

    def test_sperre_nach_fuenf_fehlversuchen(self):
        einrichten()
        c = client("192.168.1.81")
        codes = [c.post("/api/auth/login", json={"password": f"falsch-{i}"}).status_code for i in range(6)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)
        # auch das richtige Passwort kommt während der Sperre nicht durch
        self.assertEqual(c.post("/api/auth/login", json={"password": PW}).status_code, 429)

    def test_sperre_gilt_pro_adresse(self):
        einrichten()
        self.assertEqual(client("192.168.1.82").post("/api/auth/login", json={"password": PW}).status_code, 200)

    def test_sitzungscookie_ist_geschuetzt(self):
        einrichten()
        r = client("192.168.1.83").post("/api/auth/login", json={"password": PW})
        sc = r.headers.get("set-cookie", "").lower()
        for teil in ("nuorb_sess=", "httponly", "secure", "samesite=strict"):
            self.assertIn(teil, sc)
        self.assertFalse(any(r.cookies["nuorb_sess"] in json.dumps(v) or r.cookies["nuorb_sess"] in k
                             for k, v in adm.SESSIONS.items()), "Sitzung nur als Hash gespeichert")

    def test_ohne_anmeldung_gesperrt(self):
        self.assertEqual(client("192.168.1.84").get("/api/backups").status_code, 401)

    def test_abmelden(self):
        c = angemeldet("192.168.1.85")
        self.assertEqual(c.get("/api/backups").status_code, 200)
        c.post("/api/auth/logout")
        self.assertEqual(c.get("/api/backups").status_code, 401)


class Sitzungsdauer(unittest.TestCase):
    """Härtung 2: höchste Lebensdauer einer Sitzung und Obergrenze gleichzeitiger Sitzungen."""

    def test_sitzung_endet_nach_hoechstdauer_trotz_nutzung(self):
        tok = adm.new_session("192.168.1.50")
        key = adm.hashlib.sha256(tok.encode()).hexdigest()
        adm.SESSIONS[key]["start"] -= adm.SESSION_MAX_DAYS * 86400 + 60
        req = type("R", (), {"cookies": {"nuorb_sess": tok}})()
        self.assertIsNone(adm.session_of(req))
        self.assertNotIn(key, adm.SESSIONS)

    def test_gleitende_verlaengerung_nicht_ueber_die_hoechstdauer(self):
        tok = adm.new_session("192.168.1.50")
        key = adm.hashlib.sha256(tok.encode()).hexdigest()
        adm.SESSIONS[key]["start"] -= adm.SESSION_MAX_DAYS * 86400 - 3600   # noch eine Stunde
        req = type("R", (), {"cookies": {"nuorb_sess": tok}})()
        self.assertIsNotNone(adm.session_of(req))
        self.assertLessEqual(adm.SESSIONS[key]["exp"], adm.SESSIONS[key]["start"] + adm.SESSION_MAX_DAYS * 86400)

    def test_hoechstens_max_sessions_gleichzeitig(self):
        for _ in range(adm.MAX_SESSIONS + 5):
            adm.new_session("192.168.1.50")
        self.assertLessEqual(len(adm.SESSIONS), adm.MAX_SESSIONS)


class ParalleleAnmeldung(unittest.TestCase):
    """P1.1: Die Passwortprüfung blockiert den Server nicht, und parallele Fehlversuche kommen nicht an der Sperre vorbei."""

    def lauf(self, ips):
        import asyncio
        import time

        import httpx
        einrichten()

        async def go():
            hdr = {"X-NuOrb": "1", "Origin": BASE}

            async def falsch(ip, i):
                tr = httpx.ASGITransport(app=adm.app, client=(ip, 50000))
                async with httpx.AsyncClient(transport=tr, base_url=BASE, headers=hdr) as c:
                    return (await c.post("/api/auth/login", json={"password": f"falsch-{i}"})).status_code

            async def billig():
                tr = httpx.ASGITransport(app=adm.app, client=("192.168.1.199", 50000))
                async with httpx.AsyncClient(transport=tr, base_url=BASE, headers=hdr) as c:
                    t0 = time.perf_counter()
                    await asyncio.sleep(0.05)
                    r = await c.get("/api/auth/state")
                    return r.status_code, time.perf_counter() - t0 - 0.05
            return await asyncio.gather(*[falsch(ip, i) for i, ip in enumerate(ips)], billig())
        *codes, (st, dauer) = asyncio.run(go())
        return codes, st, dauer

    def test_parallele_fehlversuche_einer_adresse(self):
        codes, _, _ = self.lauf(["192.168.1.120"] * 30)
        self.assertLessEqual(codes.count(403), 5, codes)
        self.assertEqual(codes.count(403) + codes.count(429), 30, codes)

    def test_server_bleibt_bei_vielen_adressen_ansprechbar(self):
        codes, st, dauer = self.lauf([f"192.168.2.{i}" for i in range(1, 31)])
        self.assertEqual(st, 200)
        self.assertLess(dauer, 0.5, f"billiger Endpunkt brauchte {dauer:.2f} s während 30 Passwortprüfungen")


class InterneTokens(unittest.TestCase):
    """v0.7.2: Das MCP-Token des Sprachmodells darf nur /internal/assistant, das volle interne Token alles."""

    def setUp(self):
        adm.ensure_internal_token()
        self.voll = adm.INTERNAL_TOKEN_FILE.read_text().strip()
        self.mcp = adm.MCP_TOKEN_FILE.read_text().strip()

    def test_tokens_verschieden_und_nur_fuer_den_besitzer(self):
        self.assertNotEqual(self.voll, self.mcp)
        self.assertEqual(adm.MCP_TOKEN_FILE.stat().st_mode & 0o077, 0)

    def test_mcp_token_nur_fuer_assistant(self):
        self.assertTrue(adm.internal_token_ok(self.mcp, "/internal/assistant"))
        for path in ("/internal/brain", "/internal/ui", "/internal/irgendwas", ""):
            self.assertFalse(adm.internal_token_ok(self.mcp, path), path)

    def test_volles_token_fuer_alles(self):
        for path in ("/internal/assistant", "/internal/brain", "/internal/ui"):
            self.assertTrue(adm.internal_token_ok(self.voll, path))
        self.assertFalse(adm.internal_token_ok("falsch", "/internal/assistant"))

    def test_mcp_token_kommt_nicht_an_den_gehirn_schalter(self):
        c = TestClient(adm.app, base_url="https://127.0.0.1:8444", client=("127.0.0.1", 1))
        r = c.post("/internal/brain", json={"mode": "remote"}, headers={"X-NuOrb-Internal": self.mcp})
        self.assertEqual(r.status_code, 403)


class CsrfUndNetz(unittest.TestCase):
    def test_ohne_eigenen_header_abgelehnt(self):
        c = TestClient(adm.app, base_url=BASE, client=("192.168.1.90", 1))
        self.assertEqual(c.post("/api/auth/login", json={"password": PW}, headers={"Origin": BASE}).status_code, 403)

    def test_fremder_origin_abgelehnt(self):
        c = client("192.168.1.91", Origin="https://boese.example")
        self.assertEqual(c.post("/api/auth/login", json={"password": PW}).status_code, 403)

    def test_text_plain_von_fremder_seite_abgelehnt(self):
        c = TestClient(adm.app, base_url=BASE, client=("192.168.1.92", 1))
        r = c.post("/api/auth/login", content=b'{"password":"x"}',
                   headers={"Origin": "https://boese.example", "Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 403)

    def test_nur_heimnetz(self):
        c = TestClient(adm.app, base_url=BASE, client=("203.0.113.5", 1))
        self.assertEqual(c.get("/api/auth/state").status_code, 403)

    def test_internal_nur_loopback(self):
        c = TestClient(adm.app, base_url=BASE, client=("192.168.1.93", 1))
        self.assertEqual(c.post("/internal/brain", json={}, headers={"X-NuOrb-Internal": "x"}).status_code, 403)

    def test_internal_ohne_token_auch_von_loopback_abgelehnt(self):
        c = TestClient(adm.app, base_url=BASE, client=("127.0.0.1", 1))
        self.assertEqual(c.post("/internal/brain", json={}, headers={"X-NuOrb-Internal": "falsch"}).status_code, 403)

    def test_sicherheitskoepfe(self):
        r = client("192.168.1.94").get("/api/auth/state")
        self.assertIn("script-src 'self'", r.headers.get("content-security-policy", ""))
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")


class AenderungenMitSicherung(unittest.TestCase):
    SKILL = "skill:nuorb-test/demo"
    TEXT = "---\nname: demo\ndescription: Testskill\n---\nErster Stand\n"

    def test_vorschau_anwenden_und_rueckgaengig(self):
        c = angemeldet("192.168.1.100")
        p = adm.make_preview(self.SKILL, self.TEXT, "Test anlegen")
        self.assertIn("+Erster Stand", p["diff"])
        r = c.post(f"/api/change/{p['preview']}/apply")
        self.assertEqual(r.status_code, 200, r.text)
        pfad, _ = adm.file_path(self.SKILL)
        self.assertEqual(pfad.read_text(), self.TEXT)
        bid = r.json()["backup"]
        # zweite Änderung, dann die Sicherung der ersten zurückholen (Rückgängig)
        p2 = adm.make_preview(self.SKILL, self.TEXT.replace("Erster", "Zweiter"), "Test ändern")
        self.assertEqual(c.post(f"/api/change/{p2['preview']}/apply").status_code, 200)
        r = c.post(f"/api/backups/{bid}/restore")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.post(f"/api/change/{r.json()['preview']}/apply").status_code, 200)
        self.assertFalse(pfad.exists(), "Rückgängig auf den Stand vor dem Anlegen löscht den Skill wieder")

    def test_vorschau_verfaellt_nach_gebrauch(self):
        c = angemeldet("192.168.1.101")
        p = adm.make_preview("skill:nuorb-test/einmal", self.TEXT, "einmal")
        self.assertEqual(c.post(f"/api/change/{p['preview']}/apply").status_code, 200)
        self.assertEqual(c.post(f"/api/change/{p['preview']}/apply").status_code, 410)

    def test_zwischendurch_geaenderte_datei_wird_nicht_ueberschrieben(self):
        c = angemeldet("192.168.1.102")
        key = "skill:nuorb-test/konflikt"
        p = adm.make_preview(key, self.TEXT, "anlegen")
        pfad, _ = adm.file_path(key)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text("von Hermes geändert\n")
        self.assertEqual(c.post(f"/api/change/{p['preview']}/apply").status_code, 409)
        self.assertEqual(pfad.read_text(), "von Hermes geändert\n")

    def test_ungueltiger_skill_wird_abgelehnt(self):
        with self.assertRaises(adm.HTTPException):
            adm.make_preview("skill:nuorb-test/kaputt", "kein Kopf\n", "kaputt")


class Pfadpruefungen(unittest.TestCase):
    def test_unbekannte_datei(self):
        for key in ("../../etc/passwd", "skill:../x", "skill:a/../../b", "skill:A/B", "skill:/abs", ""):
            with self.subTest(key=key), self.assertRaises(adm.HTTPException):
                adm.file_path(key)

    def test_sicherung_mit_pfadanteil(self):
        c = angemeldet("192.168.1.110")
        for bid in ("..", "..%2F..%2Fetc", "gibt-es-nicht"):
            with self.subTest(bid=bid):
                self.assertIn(c.post(f"/api/backups/{bid}/restore").status_code, (404, 405))


class AuditLog(unittest.TestCase):
    """P2.12: Nutzereingaben im Audit-Log erzeugen keine neuen Spalten oder Zeilen."""

    def test_tabs_und_zeilenumbrueche_werden_maskiert(self):
        log = adm.DATA / "audit.log"
        vorher = log.read_text().count("\n") if log.exists() else 0
        adm.audit("192.168.1.60", "Zeitplan angelegt: Licht\taus\n2026-01-01T00:00:00\tadmin\tgefälschte Zeile")
        zeilen = log.read_text().splitlines()
        self.assertEqual(len(zeilen), vorher + 1)
        felder = zeilen[-1].split("\t")
        self.assertEqual(len(felder), 3)
        self.assertIn("\\n2026", felder[2])


class Schwaerzung(unittest.TestCase):
    def test_schluessel_werden_verborgen(self):
        for geheim in ("sk-" + "ant-api03-" + "x" * 40, "Bearer " + "a" * 40):  # zur Laufzeit zusammengesetzt (Datenschutz-Scan)
            with self.subTest(geheim=geheim[:12]):
                self.assertNotIn(geheim[-20:], adm.redact_text(f"Wert: {geheim} Ende"))

    def test_baum_mit_geheimen_schluesseln(self):
        out = adm.redact_tree({"api_key": "geheim1234567890", "name": "sichtbar", "liste": [{"token": "abcdefgh12345678"}]})
        self.assertEqual(out["name"], "sichtbar")
        self.assertNotEqual(out["api_key"], "geheim1234567890")
        self.assertNotEqual(out["liste"][0]["token"], "abcdefgh12345678")


class HostAllowlist(unittest.TestCase):
    """P1.5: DNS-Rebinding. Fremde Hostnamen werden abgelehnt, die Formen aus dem Heimnetz gehen durch."""
    ERLAUBT = ["192.168.1.50:{p}", "192.168.1.50", "[fe80::1]:{p}", "[::1]", "localhost:{p}", "nuorb:{p}", "nuorb.local:{p}",
               "nuorb.fritz.box", "nuorb.home.arpa", "nuorb.lan:{p}", "nuorb.example.org:{p}", "geraet.tail1234.ts.net"]
    VERBOTEN = ["boese.example:{p}", "nuorb.example.org.boese.example", "192.168.1.50.nip.io", "example.org", "ts.net.boese.example"]

    def test_hosts(self):
        for h in self.ERLAUBT:
            with self.subTest(host=h):
                self.assertNotEqual(host_anfrage(h.format(p=8444)).status_code, 421)
        for h in self.VERBOTEN:
            with self.subTest(host=h):
                self.assertEqual(host_anfrage(h.format(p=8444)).status_code, 421)


def host_anfrage(host):
    return TestClient(adm.app, base_url=BASE, client=("192.168.1.60", 1)).get("/api/auth/state", headers={"Host": host})

class ZwischendateiVorDemUmbenennen(unittest.TestCase):
    """v0.7.4-Fix: Besitz und Rechte an der Zwischendatei setzen, bevor sie das Ziel ersetzt (kein Fenster mit falschem Besitzer)."""

    def test_hook_vor_dem_umbenennen(self):
        ziel = T / "rennen" / "config.yaml"
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text("alt: 1\n")
        gesehen = []

        def hook(tmp, final):
            gesehen.append((tmp.exists(), tmp.read_text(), final == ziel, ziel.read_text()))

        alt = adm.PREPARE_TMP
        adm.PREPARE_TMP = hook
        try:
            adm.atomic_write(ziel, "neu: 2\n")
        finally:
            adm.PREPARE_TMP = alt
        self.assertEqual(gesehen, [(True, "neu: 2\n", True, "alt: 1\n")])   # Ziel noch alt, Zwischendatei fertig
        self.assertEqual(ziel.read_text(), "neu: 2\n")



if __name__ == "__main__":
    unittest.main()
