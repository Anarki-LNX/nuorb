"""NuOrb-Server: schmaler Patch an Hermes beim Bauen des Images (v0.7.4).

Hermes führt einen Werkzeugaufruf trotzdem aus, wenn die Verteilung der pre_tool_call-Hooks eine Ausnahme wirft
(agent/tool_executor.py, _pre_tool_block: „Hook failures never block“). Damit liefen NuOrbs Regeln und die Wache
(nuorb_guard) ins Leere. Nach dem Patch blockiert eine solche Ausnahme den Aufruf, mit Meldung und Protokolleintrag.
Der Build bricht ab, wenn die Stelle nicht genau einmal so im Hermes-Code steht (etwa nach einem Hermes-Update):
dann muss der Patch angesehen werden, statt still zu fehlen.
"""
import sys
from pathlib import Path

F = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/agent/tool_executor.py")
OLD = '''        return block_msg, (ref.args if modified_args is None else modified_args)
    except Exception:
        return None, ref.args
'''
NEW = '''        return block_msg, (ref.args if modified_args is None else modified_args)
    except Exception:  # NuOrb-Patch: fail closed, ein Fehler der Hook-Verteilung blockiert den Aufruf
        logger.error("pre_tool_call hook dispatch failed for %s, call blocked (NuOrb fail-closed patch)", ref.name,
                     exc_info=True)
        return ("NuOrb: the safety check for this tool failed, so the call was blocked. Nothing was run.", ref.args)
'''
src = F.read_text(encoding="utf-8")
if src.count(OLD) != 1 or "def _pre_tool_block(" not in src:
    sys.exit(f"NuOrb patch: expected block not found exactly once in {F} (Hermes changed?), refusing to build")
F.write_text(src.replace(OLD, NEW), encoding="utf-8")
print(f"NuOrb patch applied: {F}")
