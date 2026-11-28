"""Prüfung vor dem Start des Hermes-Gateways (v0.7.4): kein Betrieb mit Standardeinstellungen ohne NuOrbs Policy-Hooks.
Das Verhalten im Image (chmod 000 auf config.yaml, Neustart, kein Gateway, unhealthy) prüft .work/guard-check.sh."""
from __future__ import annotations

import json
import os
import unittest

from _lade import APP, lade, temp_ordner

T = temp_ordner("guard")
g = lade("hermes_guard_test", APP / "hermes_guard.py", {"HERMES_HOME": str(T), "NUORB_GUARD_MARK": str(T / "run/blocked")})
CMD = "/opt/nuorb/venv/bin/python -I /opt/nuorb/app/nuorb_policy.py"
GUT = f"""model: {{default: x}}
platform_toolsets:
  api_server: [web, vision, skills, cronjob, todo, memory, session_search]
hooks:
  pre_tool_call:
    - matcher: .*
      command: {CMD} pre
      timeout: 10
      fail_closed: true
  post_tool_call:
    - matcher: ^web_extract$
      command: {CMD} post
"""
ALLOW = {"approvals": [{"event": "pre_tool_call", "command": f"{CMD} pre"}, {"event": "post_tool_call", "command": f"{CMD} post"}]}


class Gateway(unittest.TestCase):
    def setUp(self):
        (T / "config.yaml").write_text(GUT)
        os.chmod(T / "config.yaml", 0o600)
        (T / "shell-hooks-allowlist.json").write_text(json.dumps(ALLOW))

    def test_vollstaendig_startet(self):
        self.assertEqual(g.problems(T), [])
        self.assertEqual(g.main(), 0)
        self.assertFalse((T / "run/blocked").exists())

    def test_erweitert_ist_erlaubt(self):
        (T / "config.yaml").write_text(GUT.replace("[web, vision, skills, cronjob, todo, memory, session_search]", "[hermes-api-server]"))
        self.assertEqual(g.problems(T), [])

    def test_unlesbar_startet_nicht(self):
        (T / "config.yaml").unlink()
        p = g.problems(T)
        self.assertTrue(p and "not readable" in p[0], p)
        self.assertEqual(g.main(), 1)
        self.assertIn("not readable", (T / "run/blocked").read_text())

    def test_ohne_hook_startet_nicht(self):
        (T / "config.yaml").write_text(GUT.split("hooks:")[0])
        self.assertTrue(any("pre_tool_call" in x for x in g.problems(T)))

    def test_ohne_fail_closed_startet_nicht(self):
        (T / "config.yaml").write_text(GUT.replace("fail_closed: true", "fail_closed: false"))
        self.assertTrue(any("fail_closed" in x for x in g.problems(T)))

    def test_ohne_allowlist_startet_nicht(self):
        (T / "shell-hooks-allowlist.json").unlink()
        self.assertTrue(any("allowlist" in x for x in g.problems(T)))

    def test_fremde_werkzeugliste_startet_nicht(self):
        (T / "config.yaml").write_text(GUT.replace("[web, vision, skills, cronjob, todo, memory, session_search]", "[web, terminal]"))
        self.assertTrue(any("api_server" in x for x in g.problems(T)))

    def test_kaputtes_yaml_startet_nicht(self):
        (T / "config.yaml").write_text("model: [unclosed\n")
        self.assertTrue(any("YAML" in x for x in g.problems(T)))


if __name__ == "__main__":
    unittest.main()
