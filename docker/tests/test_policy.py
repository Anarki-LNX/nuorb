"""Unit-Tests für die Sicherheitsregeln der Werkzeuge (S5/S6): docker/app/nuorb_policy.py und die Hilfen in nuorb_common."""
from __future__ import annotations

import json
import unittest

from _lade import APP, lade, temp_ordner

T = temp_ordner("policy")
pol = lade("nuorb_policy_test", APP / "nuorb_policy.py", {"HERMES_HOME": str(T)})
jc = lade("nuorb_common_policy_test", APP / "nuorb_common.py", {"HERMES_HOME": str(T), "NUORB_MODELS": str(T / "models")})


def pre(tool: str, args: dict | None = None, session: str = "s1"):
    return pol.decide("pre", {"tool_name": tool, "tool_input": args or {}, "session_id": session})


def post(tool: str, session: str = "s1"):
    return pol.decide("post", {"tool_name": tool, "tool_input": {}, "session_id": session})


class FremdeInhalte(unittest.TestCase):
    def setUp(self):
        for f in pol.MARKS.glob("*.json") if pol.MARKS.exists() else []:
            f.unlink()

    def test_ohne_fremde_inhalte_keine_rueckfrage(self):
        self.assertIsNone(pre("mcp__smarthome_schalten__schalten", {"name": "Licht"}))
        self.assertIsNone(pre("cronjob_manage", {"action": "create"}))

    def test_nach_webseite_braucht_aktion_bestaetigung(self):
        self.assertIsNone(post("web_extract"))
        out = pre("mcp__nuorb__name_setzen", {"name": "Hacker"})
        self.assertEqual(out["action"], "approve")
        self.assertIn("web_extract", out["message"])
        self.assertEqual(pre("cronjob_manage")["action"], "approve")
        self.assertEqual(pre("mcp__fremd__irgendwas")["action"], "approve")   # vom Nutzer hinzugefügter MCP-Server

    def test_markierung_gilt_nur_fuer_die_sitzung(self):
        post("web_search", session="a")
        self.assertIsNone(pre("cronjob_manage", session="b"))
        self.assertEqual(pre("cronjob_manage", session="a")["action"], "approve")

    def test_markierung_bleibt_ueber_den_durchgang_hinaus(self):
        post("browser_exec")
        for _ in range(3):   # jede weitere Frage derselben Sitzung
            self.assertEqual(pre("memory", {"action": "add"})["action"], "approve")

    def test_eigene_lesende_werkzeuge_markieren_nicht_und_brauchen_nichts(self):
        post("mcp__smarthome__geraete")
        post("mcp__wetter__wetter")
        self.assertIsNone(pre("cronjob_manage"))
        post("web_extract")
        self.assertIsNone(pre("mcp__smarthome__zustand"))   # lesen bleibt frei
        self.assertIsNone(pre("mcp__wetter__wetter"))

    def test_jede_rueckfrage_hat_eigenen_schluessel(self):
        post("web_extract")
        self.assertNotEqual(pre("cronjob_manage")["rule_key"], pre("cronjob_manage")["rule_key"])   # kein „immer erlauben“

    def test_alte_markierung_laeuft_ab(self):
        post("web_extract")
        f = pol.mark_file("s1")
        f.write_text(json.dumps({"t": 0, "tool": "web_extract"}))
        self.assertIsNone(pre("cronjob_manage"))


class Ausfallsicher(unittest.TestCase):
    """v0.7.4: Kann der Hook seinen Zustand nicht lesen (Ordner ohne Rechte), fällt er nicht auf „erlauben“ zurück."""

    def setUp(self):
        import os
        self.os = os
        pol.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        pol.RULES_FILE.write_text(json.dumps({"rules": []}))

    def tearDown(self):
        self.os.chmod(pol.RULES_FILE.parent, 0o700)
        self.os.chmod(pol.RULES_FILE, 0o600)

    def test_regeln_unlesbar_wirkung_braucht_bestaetigung(self):
        self.os.chmod(pol.RULES_FILE, 0o000)
        if self.os.access(pol.RULES_FILE, self.os.R_OK):
            self.skipTest("läuft als root")
        d = pre("terminal", {"command": "echo hallo"}, session="ausfall-1")
        self.assertEqual((d or {}).get("action"), "approve", d)
        self.assertIn("cannot read its tool rules", d["message"])

    def test_ordner_unlesbar_wirkung_braucht_bestaetigung(self):
        self.os.chmod(pol.RULES_FILE.parent, 0o000)
        if self.os.access(pol.RULES_FILE.parent, self.os.R_OK):
            self.skipTest("läuft als root")
        d = pre("mcp__smarthome_schalten__schalten", {"name": "Licht"}, session="ausfall-2")
        self.assertEqual((d or {}).get("action"), "approve", d)

    def test_ordner_unlesbar_lesen_bleibt_erlaubt(self):
        self.os.chmod(pol.RULES_FILE.parent, 0o000)
        self.assertIsNone(pre("web_search", {"query": "Wetter"}, session="ausfall-3"))

    def test_lesbar_wie_bisher(self):
        self.assertIsNone(pre("terminal", {"command": "echo hallo"}, session="ausfall-4"))


class Heimnetz(unittest.TestCase):
    def setUp(self):
        (T / "config.yaml").write_text("model:\n  base_url: http://192.168.1.50:11434/v1\n")

    def test_private_ziele_gesperrt(self):
        for cmd in ("curl http://192.168.178.50/", "wget 172.16.0.5", "curl http://localhost:8642/v1/runs",
                    "curl http://127.0.0.1:8444/api/auth/state", "curl http://homeassistant.local:8123", "curl http://[::1]:8443/"):
            out = pre("terminal", {"command": cmd})
            self.assertEqual((out or {}).get("action"), "block", cmd)

    def test_eingetragene_adresse_erlaubt(self):
        self.assertIsNone(pre("terminal", {"command": "curl http://192.168.1.50:11434/api/tags"}))

    def test_eingetragene_loopback_adresse_gibt_nicht_den_container_frei(self):
        (T / "config.yaml").write_text("model:\n  base_url: http://127.0.0.1:18434/v1\nstt: http://127.0.0.1:8178/v1\n")
        self.assertIsNone(pre("terminal", {"command": "curl http://127.0.0.1:18434/api/tags"}))
        for cmd in ("curl -sk https://127.0.0.1:8444/api/auth/state", "curl http://127.0.0.1:8642/v1/runs", "curl 127.0.0.1"):
            self.assertEqual((pre("terminal", {"command": cmd}) or {}).get("action"), "block", cmd)

    def test_anderer_port_derselben_adresse_gesperrt(self):
        self.assertEqual(pre("terminal", {"command": "curl http://192.168.1.50:22/"})["action"], "block")
        self.assertIsNone(pre("terminal", {"command": "ping 192.168.1.50"}))   # ohne Port: Host steht in der Konfiguration

    def test_oeffentliche_adresse_und_andere_werkzeuge_frei(self):
        self.assertIsNone(pre("terminal", {"command": "curl https://93.184.215.14/"}))
        self.assertIsNone(pre("web_search", {"query": "192.168.178.50 router"}))   # Hermes prüft web_* selbst

    def test_python_und_browser_auch(self):
        self.assertEqual(pre("execute_code", {"code": "import urllib.request; urllib.request.urlopen('http://192.168.178.50')"})["action"], "block")
        self.assertEqual(pre("browser_exec", {"code": "goto('http://172.16.0.6/admin')"})["action"], "block")


class Regeldatei(unittest.TestCase):
    """v0.7.2: rules.json mit allow / confirm / confirm_after_outside / deny, Zeitlimit, Protokoll."""

    TOOLS = ["terminal", "execute_code", "write_file", "cronjob_manage", "memory", "web_search", "web_extract",
             "skills_list", "mcp__nuorb__name_setzen", "mcp__smarthome__geraete", "mcp__smarthome_schalten__schalten",
             "mcp__wetter__wetter", "mcp__fremd__machwas", "tool_search"]

    def setUp(self):
        for f in pol.MARKS.glob("*.json") if pol.MARKS.exists() else []:
            f.unlink()
        pol.RULES_FILE.unlink(missing_ok=True)
        pol.LOG_FILE.unlink(missing_ok=True)
        (T / "config.yaml").write_text("model:\n  base_url: http://192.168.1.50:11434/v1\n")

    def tearDown(self):
        pol.RULES_FILE.unlink(missing_ok=True)

    def regeln(self, rules):
        pol.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        pol.RULES_FILE.write_text(json.dumps({"rules": rules}))

    def v071(self, tool, markiert):
        """Erwartung wie in v0.7.1: Wirkung nach fremdem Inhalt -> Karte, sonst nichts."""
        return "approve" if markiert and pol.is_effect(tool) else None

    def matrix(self):
        out = {}
        for markiert in (False, True):
            for tool in self.TOOLS:
                sess = f"m{markiert}-{tool}"
                if markiert:
                    post("web_extract", session=sess)
                out[(tool, markiert)] = (pre(tool, session=sess) or {}).get("action")
        return out

    def test_ohne_regeldatei_exakt_wie_v071(self):
        for (tool, markiert), got in self.matrix().items():
            self.assertEqual(got, self.v071(tool, markiert), f"{tool} markiert={markiert}")

    def test_leere_regelliste_wie_ohne_datei(self):
        pol.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        pol.RULES_FILE.write_text(json.dumps(jc.POLICY_TEMPLATE))   # so legt first_run sie an
        for (tool, markiert), got in self.matrix().items():
            self.assertEqual(got, self.v071(tool, markiert), f"{tool} markiert={markiert}")

    def test_deny_blockiert_immer(self):
        self.regeln([{"match": "web_search", "level": "deny"}])
        self.assertEqual(pre("web_search")["action"], "block")

    def test_confirm_auch_ohne_fremden_inhalt(self):
        self.regeln([{"match": "mcp__smarthome_schalten__*", "level": "confirm"}])
        self.assertEqual(pre("mcp__smarthome_schalten__schalten")["action"], "approve")
        self.assertIsNone(pre("mcp__smarthome__geraete"))

    def test_allow_hebt_die_karte_nach_fremdem_inhalt_auf(self):
        self.regeln([{"match": "cronjob_manage", "level": "allow"}])
        post("web_extract")
        self.assertIsNone(pre("cronjob_manage"))
        self.assertEqual(pre("memory")["action"], "approve")   # andere bleiben bei der Standardregel

    def test_erste_passende_regel_gewinnt(self):
        self.regeln([{"match": "mcp__fremd__harmlos", "level": "allow"}, {"match": "mcp__fremd__*", "level": "deny"}])
        self.assertIsNone(pre("mcp__fremd__harmlos"))
        self.assertEqual(pre("mcp__fremd__anderes")["action"], "block")

    def test_heimnetz_sperre_gilt_auch_bei_allow(self):
        self.regeln([{"match": "terminal", "level": "allow"}])
        self.assertEqual(pre("terminal", {"command": "curl http://192.168.178.50/"})["action"], "block")

    def test_zeitlimit_begrenzt_shell(self):
        self.regeln([{"match": "terminal", "level": "allow", "timeout": 30}])
        out = pre("terminal", {"command": "sleep 1", "timeout": 600})
        self.assertEqual(out["action"], "modify")
        self.assertEqual(out["args"]["timeout"], 30)
        self.assertIsNone(pre("terminal", {"command": "sleep 1", "timeout": 10}))

    def test_outside_markiert_wie_eine_webseite(self):
        self.regeln([{"match": "mcp__smarthome__geraete", "level": "allow", "outside": True}])
        post("mcp__smarthome__geraete")
        self.assertEqual(pre("cronjob_manage")["action"], "approve")

    def test_ungueltige_regeln_werden_uebersprungen_und_protokolliert(self):
        self.regeln([{"match": "terminal", "level": "verbieten"}, {"match": "web_search", "level": "deny"}])
        self.assertIsNone(pre("terminal"))   # ungültige Regel: Standardwert
        self.assertEqual(pre("web_search")["action"], "block")
        self.assertIn("rules-problem", pol.LOG_FILE.read_text())

    def test_kaputte_datei_gibt_standardwerte(self):
        pol.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        pol.RULES_FILE.write_text("{kaputt")
        post("web_extract")
        self.assertEqual(pre("cronjob_manage")["action"], "approve")
        self.assertIsNone(pre("web_search"))

    def test_protokoll_schwaerzt_und_hasht(self):
        pre("terminal", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' https://example.org", "api_key": "geheim"},
            session="meine-sitzung")
        line = json.loads(pol.LOG_FILE.read_text().splitlines()[-1])
        self.assertNotIn("abcdefghijklmnop", line["args"])
        self.assertNotIn("geheim", line["args"])
        self.assertNotIn("meine-sitzung", json.dumps(line))
        self.assertEqual(pol.LOG_FILE.stat().st_mode & 0o077, 0)

    def test_pruefung_der_regeln_fuer_das_admin_panel(self):
        _, err = pol.parse_rules({"rules": [{"match": "x", "level": "allow", "timeout": 0}, {"match": "y", "level": "allow", "foo": 1}]})
        self.assertEqual(len(err), 2)
        rules, err = pol.parse_rules({"rules": [{"match": "x", "level": "confirm", "timeout": 60, "note": "ok"}]})
        self.assertEqual((len(rules), err), (1, []))


class Konfiguration(unittest.TestCase):
    def test_enger_standard_und_hooks(self):
        data: dict = {"hooks": {"pre_tool_call": [{"command": "/eigener/hook.sh"}]}}
        self.assertTrue(jc.apply_tools_policy(data, "narrow"))
        self.assertEqual(data["platform_toolsets"]["api_server"], jc.TOOLSETS["narrow"])
        self.assertNotIn("terminal", data["platform_toolsets"]["api_server"])
        pre_h = data["hooks"]["pre_tool_call"]
        self.assertTrue(pre_h[0]["command"].endswith("nuorb_policy.py pre") and pre_h[0]["fail_closed"] is True)
        self.assertEqual(pre_h[1]["command"], "/eigener/hook.sh")   # eigener Hook bleibt
        self.assertTrue(data["hooks"]["post_tool_call"][0]["command"].endswith("nuorb_policy.py post"))
        self.assertFalse(jc.apply_tools_policy(data, "narrow"))   # zweiter Lauf ändert nichts

    def test_erweitert(self):
        data: dict = {}
        jc.apply_tools_policy(data, "extended")
        self.assertEqual(data["platform_toolsets"]["api_server"], ["hermes-api-server"])
        self.assertEqual(len(data["hooks"]["pre_tool_call"]), 1)   # Regeln gelten auch erweitert

    def test_allowlist_nur_fuer_die_eigenen_hooks(self):
        jc.allow_policy_hooks(T)
        jc.allow_policy_hooks(T)
        d = json.loads((T / "shell-hooks-allowlist.json").read_text())
        self.assertEqual(sorted(e["event"] for e in d["approvals"]), ["post_tool_call", "pre_tool_call"])

    def test_zustand_ohne_datei_ist_eng(self):
        self.assertEqual(jc.tools_state()["mode"], "narrow")


if __name__ == "__main__":
    unittest.main()
