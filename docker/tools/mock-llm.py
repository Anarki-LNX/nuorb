#!/usr/bin/env python3
"""Mini-Mock für Tests: tut so, als wäre es Ollama (/api/version, /api/tags) und ein OpenAI-kompatibles LLM
(/v1/models, /v1/chat/completions mit und ohne Stream). Antwortet immer mit einem festen Satz plus Echo.
Keine GPU, kein Netz. Start: python3 docker/tools/mock-llm.py [port]   (Standard 18434, nur 127.0.0.1)
Werkzeug-Test: Enthält die Nutzerfrage [[tool:<endung> <json>]], ruft der Mock das angebotene Werkzeug auf, dessen Name
auf <endung> endet (z. B. [[tool:smarthome_schalten__schalten {"name":"Küchenlicht","aktion":"aus"}]]); nach dem
Werkzeug-Ergebnis antwortet er mit „Erledigt: <Ergebnis>“. Mehrere Marker werden nacheinander aufgerufen, ein Werkzeug
pro Ergebnis (für den Test „fremde Inhalte, dann Aktion“). GET /mock/last-tools zeigt die Werkzeuge der letzten Anfrage.
"""
import re
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18434
MODEL = "mock-nuorb"
# Zweite Bauart für Tests der OpenAI-kompatiblen Server (docs/llm-backends.md): MOCK_KIND=llamacpp -> kein /api/*,
# dafür /props (n_ctx) und owned_by "llamacpp"; MOCK_API_KEY=… verlangt einen Bearer-Key.
import os  # noqa: E402
KIND = os.environ.get("MOCK_KIND", "ollama")
API_KEY = os.environ.get("MOCK_API_KEY", "")


def answer(msgs):
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    c = last.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return f"Testantwort vom Mock-Gehirn. Du hast gefragt: {str(c)[-80:]}"


LAST_TOOLS: list[str] = []
MISSING: list[str] = []   # verlangte Werkzeuge, die Hermes in einer Anfrage nicht angeboten hat


def tool_call(body):
    """Werkzeugaufruf, solange die letzte Nutzerfrage noch Test-Marker ohne Ergebnis trägt (der Reihe nach)."""
    msgs = body.get("messages") or []
    if body.get("tools"):   # Nebenanfragen von Hermes (Titel o. ä.) ohne Werkzeuge nicht mitzählen
        LAST_TOOLS[:] = [t.get("function", {}).get("name", "") for t in body["tools"]]
    i_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    if i_user < 0:
        return None
    c = msgs[i_user].get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    marker = re.findall(r"\[\[tool:(\S+) (\{.*?\})\]\]", str(c or ""))
    done = sum(1 for m in msgs[i_user + 1:] if m.get("role") == "tool")
    if done >= len(marker):
        return None
    end, args = marker[done]
    names = [t.get("function", {}).get("name", "") for t in body.get("tools") or []]
    name = next((n for n in names if n.endswith(end)), None)
    if name:
        return {"name": name, "arguments": args}
    if "tool_call" in names:   # Hermes' Tool Search: MCP-Werkzeuge stehen hinter der Brücke tool_call
        listing = " ".join(str(t.get("function", {}).get("description", "")) for t in body.get("tools") or [])
        m = re.search(r"\b(\w*" + re.escape(end) + r")\b", listing)
        real = m.group(1) if m else ("mcp__" + end if "__" in end else end)
        return {"name": "tool_call", "arguments": json.dumps({"calls": [{"name": real, "arguments": json.loads(args)}]})}
    MISSING.append(end)
    return {"missing": end, "names": names}


def after_tool(msgs):
    if msgs and msgs[-1].get("role") == "tool":
        c = msgs[-1].get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return f"Erledigt: {str(c)[:1500]}"
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, f, *a):
        print("mock", f % a, flush=True)

    def js(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def authed(self):
        if API_KEY and self.headers.get("Authorization", "") != f"Bearer {API_KEY}":
            self.js(401, {"error": {"message": "Invalid API Key", "type": "authentication_error"}})
            return False
        return True

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if KIND == "llamacpp":
            if p == "/health":
                return self.js(200, {"status": "ok"})
            if p.startswith("/api/"):
                return self.js(404, {"error": "not found"})
            if not self.authed():
                return
            if p == "/props":
                return self.js(200, {"default_generation_settings": {"n_ctx": 65536}, "build_info": "b0-mock"})
            if p in ("/v1/models", "/models"):
                return self.js(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "llamacpp"}]})
        if p == "/mock/release":   # Update-Prüfung des Admin-Panels (NUORB_UPDATE_URL in .env.test)
            return self.js(200, {"tag_name": "v99.0.0", "html_url": "http://127.0.0.1/kein-github", "published_at": "2026-12-01T00:00:00Z"})
        if p == "/mock/last-tools":
            return self.js(200, {"tools": LAST_TOOLS, "missing": MISSING})
        if p == "/api/version":
            return self.js(200, {"version": "0.0.0-mock"})
        if p == "/api/tags":
            return self.js(200, {"models": [{"name": MODEL, "size": 1}]})
        if p == "/api/ps":
            return self.js(200, {"models": []})
        if p in ("/v1/models", "/models"):
            return self.js(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}]})
        self.js(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = self.path.split("?")[0].rstrip("/")
        if KIND == "llamacpp" and (p.startswith("/api/") or not self.authed()):
            if p.startswith("/api/"):
                self.js(404, {"error": "not found"})
            return
        if p == "/api/generate":
            return self.js(200, {"done": True})
        if p == "/api/show":  # Hermes fragt die echte Kontextgröße ab
            return self.js(200, {"parameters": "num_ctx                        65536",
                                 "model_info": {"general.architecture": "mock", "mock.context_length": 65536},
                                 "capabilities": ["completion", "tools"]})
        if not p.endswith("/chat/completions"):
            return self.js(404, {"error": "not found"})
        msgs = body.get("messages") or []
        tc = tool_call(body)
        if tc and "missing" in tc:
            print("mock: Werkzeug fehlt", tc["missing"], "angeboten:", tc["names"][:40], flush=True)
            tc = None
        text = after_tool(msgs) or answer(msgs)
        cid, now = f"chatcmpl-mock{int(time.time()*1000)}", int(time.time())
        usage = {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}
        if tc:
            print("mock: Werkzeugaufruf", tc["name"], str(tc["arguments"])[:120], flush=True)
            call = {"id": f"call_{int(time.time()*1000)}", "type": "function", "function": tc}
            if not body.get("stream"):
                return self.js(200, {"id": cid, "object": "chat.completion", "created": now, "model": MODEL,
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                                                  "finish_reason": "tool_calls"}], "usage": usage})
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": MODEL}
            for d in ({"role": "assistant", "content": None, "tool_calls": [{"index": 0, **call}]},):
                self.wfile.write(b"data: " + json.dumps({**base, "choices": [{"index": 0, "delta": d, "finish_reason": None}]}).encode() + b"\n\n")
            self.wfile.write(b"data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": usage}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            return
        if not body.get("stream"):
            return self.js(200, {"id": cid, "object": "chat.completion", "created": now, "model": MODEL,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                              "finish_reason": "stop"}], "usage": usage})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        def ev(d):
            self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n"); self.wfile.flush()
        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": MODEL}
        ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        for w in text.split(" "):
            ev({**base, "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]})
        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
