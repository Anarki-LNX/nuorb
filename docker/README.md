# NuOrb: Server als Docker-Container

Dein eigener Sprachassistent in **einem** Container: Hermes Agent als Gehirn-Framework, die NuOrb-Oberfläche mit Orb,
ein Admin-Panel, Spracherkennung (Parakeet, läuft auf der CPU) und Sprachausgabe (Piper). Denken lässt du entweder
dein eigenes **Ollama** (z. B. Gaming-PC), **Claude** über die Anthropic-API oder **beides** mit automatischem Wechsel.

Die **NuOrb-App für Android** und jeder Browser im Heimnetz verbinden sich damit, nachdem sie einmal gekoppelt
wurden (QR-Code oder Code im Admin-Panel).

> **Am einfachsten:** `curl -fsSL https://github.com/Anarki-LNX/nuorb/releases/latest/download/install.sh | bash -s -- --lang de`
> Prüft Docker und Firewall, fragt kurz nach, startet NuOrb und zeigt Adresse und Einrichtungscode.
> Ausführliche Anleitung: [INSTALL.de.md](../INSTALL.de.md) · [INSTALL.md](../INSTALL.md) (English).
> Von Hand: `.env` anlegen → `docker compose up -d` → `https://<server>:8444` öffnen → Einrichtungscode eingeben →
> Gehirn wählen → Smartphone koppeln. Dauert etwa 10 Minuten, davon die meiste Zeit Downloads.

---

## Voraussetzungen

| | Minimum | Empfohlen |
|---|---|---|
| Architektur | amd64 (x86-64) oder arm64 (z. B. Raspberry Pi 5) | amd64 |
| CPU | 4 Kerne | 6+ Kerne (Spracherkennung läuft auf der CPU, ~1,5 s pro Satz) |
| RAM | 4 GB frei | 6 GB |
| Platte | 8 GB | 15 GB |
| Software | Docker Engine 24+ mit Compose v2 (`docker compose version`) | aktuelle Version |
| Netz | Server und Smartphone im selben Heimnetz **oder** per VPN verbunden | feste IP für den Server |
| Gehirn | Ollama im Heimnetz **oder** ein Anthropic-API-Schlüssel | beides |

**Downloads:** Image ca. 1,2 GB (entpackt ~3,5 GB). Beim ersten Start lädt der Container zusätzlich
**~530 MB** Sprachmodelle (Spracherkennung Parakeet 464 MB von GitHub, Stimme ~60 MB von Hugging Face). Danach läuft
die Sprachverarbeitung ohne Internet.

**Ollama:** Hermes verlangt mindestens **64k Token Kontext** und lehnt kleinere Modelle ab. Am Ollama-Server
`OLLAMA_CONTEXT_LENGTH=65536` setzen (oder eine Modellvariante mit `PARAMETER num_ctx 65536` anlegen) und Ollama im
LAN erreichbar machen (`OLLAMA_HOST=0.0.0.0`, Port 11434 nur für den NuOrb-Server freigeben). Geeignet sind Modelle
mit Werkzeugnutzung, z. B. Gemma 4 26B oder gpt-oss 20B (16 GB VRAM).

**Claude:** Schlüssel unter <https://console.anthropic.com>, dort ein Ausgabenlimit setzen. Eine kurze Sprachfrage
kostet mit Haiku etwa 0,1-0,5 Cent.

## Installation

### Wohin? Proxmox oder PC

* **Proxmox, VM (empfohlen):** Debian-13-VM mit 4-6 Kernen, 6 GB RAM, 20 GB Platte. Darin Docker installieren:
  `curl -fsSL https://get.docker.com | sh`.
* **Proxmox, LXC:** Debian-13-Container, *unprivilegiert*, unter *Optionen → Features* `nesting=1` und `keyctl=1`.
  Darin Docker wie oben. (Docker im LXC ist von Proxmox nicht offiziell unterstützt, läuft aber meist; sonst VM.)
* **PC/Server:** Linux mit Docker Engine, oder Windows/macOS mit Docker Desktop.

### Fünf Schritte

```bash
# 1. Dateien holen
git clone https://github.com/Anarki-LNX/nuorb && cd nuorb/docker

# 2. Einstellungen: mindestens NUORB_HOSTS (IP/Name des Servers), Sprache, Zeitzone
cp .env.example .env && nano .env

# 3. Starten (lädt das fertige Image; ist es nicht abrufbar, baut Compose es lokal, ~10 min)
docker compose up -d

# 4. Einrichtungscode anzeigen
docker compose logs nuorb | grep -i "setup code"
```

5. Im Browser **`https://<server-ip>:8444`** öffnen. Beim ersten Mal warnt der Browser vor dem Zertifikat
   (selbst ausgestellt, siehe unten) → „Erweitert → Trotzdem fortfahren“. Code eingeben, Passwort festlegen. Danach
   führt die **Einrichtung** durch Sprache/Stimme, Gehirn (Ollama-Adresse mit „Verbindung testen“ und Modellauswahl,
   Claude-Schlüssel oder beides) und zeigt den Fortschritt der Modell-Downloads.

`<server-ip>` ist die Adresse des Rechners bzw. der VM/des LXC, auf dem Docker läuft (`hostname -I`).

### Smartphone koppeln

1. Im Admin-Panel **Gerät koppeln → Neuen Kopplungscode erzeugen**.
2. **App:** NuOrb-App öffnen → „Server hinzufügen“ → **QR-Code scannen**. Ohne Kamera: Adresse
   (`https://<server-ip>:8443`) und Code von Hand eingeben; die App zeigt dann den Zertifikats-Fingerabdruck, der
   mit dem im Admin-Panel übereinstimmen muss.
3. **Browser** (PC, Tablet): `https://<server-ip>:8443` öffnen und den Code eintippen.

Ein Code gilt 10 Minuten und nur einmal. Gekoppelte Geräte stehen im Admin-Panel und lassen sich dort entkoppeln.

### Updates und Sicherung

```bash
docker compose pull && docker compose up -d        # neues Image, Daten bleiben
docker run --rm -v nuorb_data:/d -v "$PWD":/b debian tar czf /b/nuorb-data.tgz -C /d .   # Sicherung
```

### Umstieg von einer älteren Installation (bis v0.6.x)

Ab v0.7.0 tragen Volumes, Pfade, Umgebungsvariablen und Dienste durchgehend den Namen NuOrb. Bestehende Installationen
zieht das Umzugsskript aus dem Release v0.7.0 um. Es legt vorher eine Sicherung an, lässt die alten Daten liegen und
übernimmt Passwort, Gedächtnis und gekoppelte Geräte. Die Schritte stehen in den Release-Notes. Browser müssen danach
einmal neu koppeln.

## Was wo liegt

| Volume | Inhalt |
|---|---|
| `data` → `/opt/data` | Hermes-Konfiguration, Schlüssel (`.env`, im Admin nur als „gesetzt …1234“ sichtbar), Gedächtnis, Sitzungen, Zeitpläne, Skills; unter `nuorb/`: Admin-Passwort-Hash, Sicherungen, Zertifikate, gekoppelte Geräte, Protokolle |
| `models` → `/opt/models` | Stimmen (`voices/`) und Spracherkennung (`stt/`) |

## Netz und Sicherheit

| Port | Dienst |
|---|---|
| 8443 | Oberfläche und App-Schnittstelle (nur gekoppelte Geräte) |
| 8444 | Admin-Panel (Passwort) |

* **Kein Port ins Internet.** Nicht im Router freigeben. Unterwegs: VPN ins Heimnetz (WireGuard, Tailscale,
  Fritzbox-VPN). Zugriffe sind nur aus privaten Netzen und Tailscale (100.64.0.0/10) erlaubt.
* **Kopplung:** Ohne gekoppeltes Gerät liefert der Server nichts außer der Kopplungsseite. Auf dem Server liegen nur
  Hashes der Gerätetoken.
* **Bestätigungskarten:** Vor gefährlichen Aktionen fragt NuOrb nach (`approvals.mode: manual`).
* **Container (ab v0.7.2):** Das Dateisystem des Images ist schreibgeschützt (`read_only`). Geschrieben wird nur in
  die Volumes und in tmpfs für `/run`, `/tmp` und `/var/tmp`. Speichergrenze 4 GB (`NUORB_MEM_LIMIT`), höchstens
  2048 Prozesse, keine neuen Rechte zur Laufzeit. Von allen Linux-Fähigkeiten bleiben nur sieben: `SETUID` und `SETGID`
  (die Dienste als eigener Benutzer starten), `KILL` (Dienste beenden), `CHOWN`, `FOWNER` und `DAC_OVERRIDE` (Besitz und
  Rechte der Daten setzen, für das Admin-Panel) und `DAC_READ_SEARCH` (nur lesen, für die Oberfläche). Die Dienste selbst
  laufen nicht als root.
* **Schutzregeln ausfallsicher (ab v0.7.4):** Werkzeuge laufen nur, wenn NuOrbs Regeln nachweislich aktiv sind. Kann
  Hermes `config.yaml` nicht lesen oder fehlen NuOrbs Hooks, startet das Gateway nicht (Healthcheck „unhealthy“, klare
  Meldung im Protokoll). Ist der Hook in einem laufenden Hermes nicht aktiv, sperrt die Wache `nuorb_guard` jeden
  Werkzeugaufruf. Ein Fehler beim Prüfen blockiert den Aufruf, statt ihn durchzulassen (schmaler Patch an Hermes,
  offengelegt in `patches/` und im Image-Label `org.nuorb.hermes.modified`). Kann der Hook seine Regeln nicht lesen,
  brauchen Aktionen mit Wirkung eine Bestätigung.
* **Getrennte Benutzer (ab v0.7.4):** Hermes und damit alle Werkzeuge des Sprachmodells laufen als `hermes`, ohne
  Fähigkeiten. Admin-Panel und Oberfläche laufen als eigener Benutzer `nuorb`. Die Geheimnisse von NuOrb (Admin-Passwort,
  Sitzungen, interne Schlüssel, CA- und Server-Schlüssel, gekoppelte Geräte) gehören nur `nuorb`. Eine eingeschleuste
  Anweisung an das Sprachmodell kommt also selbst mit eingeschalteter Shell nicht an sie heran. Das Admin-Panel hat dafür
  `CHOWN`, `FOWNER` und `DAC_OVERRIDE`: Es bearbeitet die Dateien von Hermes und gibt sie danach an `hermes` zurück. Die
  Oberfläche hat nur `DAC_READ_SEARCH`, weil Hermes sein Datenverzeichnis beim Start auf 0700 setzt und die Oberfläche
  darin `config.yaml`, `.env` (Schlüssel für Hermes) und das Gedächtnis lesen muss. Sie kann Dateien von Hermes also lesen,
  aber nicht ändern. Die Umstellung passiert beim Start automatisch, auch für bestehende Installationen. Zurück geht es
  mit `install.sh --rollback`, ein älteres Image auf demselben Volume stellt die Besitzer ebenfalls zurück (ab v0.7.3).
* **Werkzeuge (ab v0.7.1):** Das Sprachmodell bekommt standardmäßig nur, was ein Sprachassistent braucht: Websuche,
  Webseiten, Bilder, Gedächtnis, Erinnerungen, Skills und die NuOrb-Werkzeuge (Smart Home, Wetter, Name). Shell,
  Python, Dateien, Browser und Unteragenten sind aus. Einschalten im Admin-Panel unter **Werkzeuge**, mit Warnung:
  Eine Webseite kann versteckte Anweisungen enthalten, und mit Shell- und Dateizugriff könnte das Modell dann
  Passwörter und Schlüssel dieses Servers lesen.
* **Regeln außerhalb des Modells:** Ein Hook (`app/nuorb_policy.py`) prüft jeden Werkzeugaufruf. Hat der Assistent
  fremde Inhalte gelesen (Websuche, Webseite, Datei, Bild, ein hinzugefügter MCP-Server), braucht danach jede Aktion
  mit Wirkung eine Bestätigungskarte, für das ganze Gespräch. Shell, Python und Browser dürfen keine Adressen im
  Heimnetz oder im Container ansprechen, außer den eingetragenen (Gehirn, MCP, Smart Home).
* **Keine Telemetrie.** Nach außen geht nur: Claude (Gesprächstext, wenn gewählt), die einmaligen Modell-Downloads
  und ab v0.7.3 die Update-Prüfung: ein Abruf bei github.com (neuestes NuOrb-Release), nur wenn das Admin-Panel offen
  ist, höchstens alle 6 Stunden. Abschaltbar unter „Über“, ganz aus mit `NUORB_UPDATE_CHECK=0`. onnxruntime (ab 1.32) würde von sich aus Nutzungsdaten an Microsoft senden; das Image
  schaltet das für jeden Prozess ab (`ORT_DISABLE_TELEMETRY=1`). Nachprüfen, komplett offline:
  `tools/telemetry-check.sh ghcr.io/anarki-lnx/nuorb:latest`.
* **Schlüssel** im Admin-Panel eingeben (oder einmalig per `.env`; danach dort löschen).

### Zertifikat

Standard (`NUORB_TLS=selfsigned`): Der Container erzeugt eine eigene kleine Zertifizierungsstelle und ein
Zertifikat für die Namen in `NUORB_HOSTS`. Die App bekommt den **Fingerabdruck dieser CA im QR-Code** und vertraut
genau diesem Server, ohne Warnung. Browser warnen einmal; wer das nicht will, importiert die CA
(`https://<server-ip>:8443/nuorb-ca.crt`) und vergleicht den Fingerabdruck mit dem im Admin-Panel.

**Heim-CA nur fürs Heimnetz:** Die selbst erzeugte CA (ab v0.7.1) trägt eine Namensbeschränkung. Sie gilt nur für
Heimnetz-Namen (`.local`, `.lan`, `.home`, `.home.arpa`, `.internal`, `.fritz.box`), private IP-Adressen und die
DNS-Namen, die beim ersten Start in `NUORB_HOSTS` stehen. Auf dem Smartphone installiert, kann sie also keine fremden
Webseiten ausstellen. Eigene öffentliche Namen deshalb vor dem ersten Start eintragen. Ein später ergänzter Name, den
die CA nicht abdeckt, kommt nicht ins Zertifikat, das Protokoll nennt ihn. Eine ältere CA ohne Beschränkung bleibt
unverändert, damit niemand neu koppeln muss.

Eigenes Zertifikat (z. B. `tailscale cert`, eigene CA, Let's Encrypt per DNS): `NUORB_TLS=custom` und den Ordner
mit `fullchain.pem`/`privkey.pem` in `compose.yaml` unter `/certs` einbinden. Hinter einem Reverse-Proxy (z. B.
Caddy): `NUORB_TLS=off`, `NUORB_BIND=127.0.0.1`, `NUORB_PUBLIC_URL` auf die Proxy-Adresse und
`NUORB_TRUSTED_PROXY` auf die Adresse des Proxys, damit Allowlist und Fehlversuchssperre die echten Client-Adressen sehen.
Eigene Hostnamen des Proxys gehören nach `NUORB_ALLOWED_HOSTS`.

**Container-Härtung (ab v0.7.1):** `compose.yaml` startet NuOrb mit `no-new-privileges`, ohne alle Linux-Fähigkeiten
außer den sechs, die der Start braucht (Besitz setzen, zum Nutzer `hermes` wechseln, Dienste beenden), und mit einer
Obergrenze für Prozesse. Die NuOrb-Dienste selbst laufen als `hermes`. Wer für eigene Erweiterungen mehr braucht,
ergänzt das in einer eigenen `compose.override.yaml`.

**Netz-Allowlist:** `NUORB_ALLOWED_NETS` ist ein Zusatzschutz, Passwort und Kopplung schützen auch ohne sie. Ohne
Angabe gelten die üblichen Heimnetze, VPN-Adressen und der eigene Docker-Gateway. Reicht Docker die Ports per Proxy
weiter (z. B. bei IPv6), erscheinen alle Clients mit der Gateway-Adresse, dann gilt auch die Fehlversuchssperre für
alle gemeinsam. Von Docker veröffentlichte Ports umgehen ufw: mit `NUORB_BIND` (oder `install.sh --bind`) lässt sich
NuOrb an eine Adresse binden, z. B. die Heimnetz-Adresse. Wer NuOrb unterwegs über ein VPN nutzt, bindet nicht oder an
die VPN-Adresse.

**Server automatisch finden (mDNS):** Mit `network_mode: host` und `NUORB_MDNS=1` kündigt sich der Server als
`_nuorb._tcp` im Heimnetz an, und die App listet ihn. Im normalen Bridge-Netz geht das nicht; dann IP eintragen.

## Gehirn und Sprache

| Modus | Bedeutung |
|---|---|
| Lokal | Nur dein lokaler Server (Ollama, llama.cpp, vLLM, LM Studio …). Nichts verlässt dein Netz. |
| Automatisch | Lokaler Server; ist er nicht erreichbar, übernimmt Claude. |
| Remote | Nur Claude (Haiku, Sonnet oder Opus). |

Statt Ollama geht **jeder OpenAI-kompatible Server** (llama.cpp `llama-server`, vLLM, LM Studio, LocalAI): in der
Einrichtung Typ „OpenAI-kompatibel“ wählen, Adresse mit Port und ggf. API-Key eintragen. Startbefehle mit Tool-Calling
und 64k Kontext: [INSTALL.de.md](../INSTALL.de.md#llamacpp-vllm-lm-studio-statt-ollama). In der `.env`: `NUORB_INIT_LLM_TYPE=openai`,
`NUORB_INIT_OLLAMA_URL`, `NUORB_INIT_OLLAMA_MODEL`, `NUORB_INIT_LLM_API_KEY`.

Umschalten im Admin unter „Modell & Fallback“ oder in der Oberfläche (Abzeichen oben antippen). Eigenes Ollama im
selben Compose-Projekt: `docker compose --profile ollama up -d`, Adresse dann `http://ollama:11434`.

**Name:** Das Produkt heißt NuOrb, das Weckwort „Hey NuOrb“ (`BRAND_NAME`). Wie du den Assistenten ansprichst,
legst du in der Einrichtung fest oder einfach im Gespräch: „Ab jetzt heißt du Max.“ Beim ersten Gespräch stellt er
sich vor und fragt auch nach deinem Namen.

Sprache **Deutsch oder Englisch** wählst du in der Einrichtung (Oberfläche: `UI_LANG=auto` folgt der Gerätesprache; Admin-Webinterface: `ADMIN_LANG=en` oder `de`, im Browser umschaltbar); dazu passende Stimmen: Thorsten (Deutsch) und Joe (Englisch), beide CC0 und auch kommerziell nutzbar. Bis v0.6.2
angebotene Stimmen ohne diese Freigabe (Alan, Ryan, Amy, Kerstin low, Thorsten high) stellt das Update automatisch um
und löscht ihre Dateien; Details in `THIRD_PARTY_LICENSES.md`. 

Weckwort „Hey NuOrb“: Die App bringt ihre Modelle selbst mit. Optional kann der Server sie liefern, wenn die Dateien
(`frei_melspectrogram.tflite`, `frei_embedding_model.tflite`, `hey_nuorb_de.tflite`, `hey_nuorb_en.tflite`) im Volume
unter `nuorb/wakeword/` liegen. Die App prüft jede Datei per Prüfsumme.

## Smart Home (optional)

NuOrb kann Licht, Steckdosen, Schalter, Szenen und Sensoren aus **Home Assistant** und/oder **ioBroker** abfragen und
schalten. Standardmäßig ist das **aus**.

**Einschalten in der Verwaltung:** „Smart Home“ öffnen, Adresse und Zugang eintragen (Home Assistant: Long-Lived
Access Token aus dem HA-Profil; ioBroker: Adapter `rest-api` mit Benutzer/Passwort oder Token), „Verbindung testen“,
dann einschalten. Der Zugang landet in `/opt/data/smarthome.env` (nur für den Besitzer lesbar) und wird nie angezeigt.

**Oder per `.env`** (z. B. für automatische Installationen): Zugangsdatei ins Daten-Volume legen oder als
Docker-Secret einbinden (`NUORB_SMARTHOME_ZUGANG=/run/secrets/…`), dann `NUORB_SMARTHOME=1` und `docker compose up -d`.
`NUORB_SMARTHOME=0` schaltet beim Start aus; leer lässt die Einstellung der Verwaltung stehen.

```bash
docker compose exec -T nuorb sh -c 'umask 077; cat > /opt/data/smarthome.env' <<'EOF'
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<Long-Lived Access Token>
EOF
```

Lesen läuft ohne Rückfrage, **Schalten zeigt immer eine Bestätigungskarte**. Der Container muss Home Assistant
(Port 8123) bzw. ioBroker (8093) im Netz erreichen.

**Ehrlich zur Zuverlässigkeit:** Mit **Claude** (z. B. Haiku) klappt das zuverlässig (im Test 36 von 37 Fragen
inhaltlich richtig). Mit einem **lokalen Ollama-Modell** ist es eingeschränkt: im Test etwa 10 von 16 richtig, typische
Fehler sind „Ich schalte jetzt …“ ohne tatsächlichen Aufruf. Lokal unbedingt unter „Model & Fallback“ die
Werkzeugsuche (`tools.tool_search`) ausschalten.

## Nicht enthalten

Steuerung des Xiaomi-Smartphones (NixOS-Kiosk) und Haus-/Wetterstation-Anbindungen des Autors. Weitere eigene
Werkzeuge bindest du im Admin unter „MCP-Server“ an.

## Fehlersuche

```bash
docker compose ps                    # läuft er? (healthy)
docker compose logs -f nuorb        # alles; Zeilen [nuorb-init], [nuorb-models], [nuorb-admin], [pair], Hermes
docker compose exec nuorb /command/s6-svstat /run/service/nuorb-ui    # einzelner Dienst
```

| Problem | Lösung |
|---|---|
| Browser: „Verbindung nicht sicher“ | Erwartet beim selbst ausgestellten Zertifikat, einmal bestätigen (oder CA importieren). |
| Einrichtungscode nicht im Log | `docker compose exec nuorb cat /opt/data/nuorb/admin/setup-code.txt` |
| „Nur im Heimnetz erreichbar“ | Zugriff kommt aus einem nicht erlaubten Netz → `NUORB_ALLOWED_NETS` ergänzen. |
| Spracherkennung offline nach dem Start | Modelle laden noch (Fortschritt in der Einrichtung). Meldet die Einrichtung „keine Internetverbindung“: siehe unten. |
| NuOrb antwortet „hat noch kein Gehirn“ | Einrichtung im Admin abschließen. |
| Antworten leer oder Fehler „context window … below 64,000“ | Ollama-Kontext zu klein, siehe oben. |
| App: Zertifikatsfehler | Server-IP fehlt in `NUORB_HOSTS` → eintragen, `docker compose up -d`, neu koppeln. |
| Ollama „nicht erreichbar“ | Die IP des Ollama-Rechners eintragen (z. B. `http://192.168.1.50:11434`), auch wenn Ollama auf demselben PC läuft; Ollama mit `OLLAMA_HOST=0.0.0.0` starten. `.local`-Namen lösen im Container oft nicht auf. Testen: `docker compose exec nuorb curl -s <adresse>/api/version`. Firewall siehe unten; die Einrichtung zeigt bei Timeout fertige Befehle (ufw, firewalld, Windows) mit der IP dieses Servers (`NUORB_HOST_IP`, sonst die erste IP aus `NUORB_HOSTS`). Alternative für denselben PC: in `compose.yaml` die `extra_hosts`-Zeile aktivieren, dann `http://host.docker.internal:11434`. |
| Port 8443/8444 belegt | `NUORB_UI_PORT_PUBLIC` / `NUORB_ADMIN_PORT_PUBLIC` ändern. |
| Uhrzeit falsch | `TZ` in der `.env` setzen. |

### Kein Netz im Container (ufw, firewalld)

Zeigt die Einrichtung „Der Container hat keine Internetverbindung“, steht der Modell-Download bei „Verbinde …“ oder
erreicht NuOrb dein Ollama nicht, blockiert meist die Firewall des Rechners den weitergeleiteten Verkehr der Container.

* **ufw** (Ubuntu, CachyOS, Arch …): Standard ist `DEFAULT_FORWARD_POLICY="DROP"`. Freigeben:
  ```bash
  sudo ufw default allow routed && sudo ufw reload
  ```
  Ollama auf demselben PC zusätzlich vom Docker-Netz erlauben: `sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp`.
* **firewalld** (Fedora, openSUSE): `sudo firewall-cmd --permanent --zone=trusted --add-interface=docker0 && sudo firewall-cmd --reload`
  (Compose-Netze heißen `br-…`; bei Bedarf ebenso hinzufügen).
* **Docker selbst ohne Netz:** Stehen in `/etc/docker/daemon.json` die Schlüssel `"iptables": false` oder
  `"bridge": "none"`, haben Container gar kein Netz. Beide Schlüssel entfernen, dann `sudo systemctl restart docker`.
  Docker legt danach eigene Firewall-Regeln an; veröffentlichte Ports (8443/8444) sind dann im Heimnetz erreichbar.
* **Ollama auf demselben PC, Test meldet „ConnectTimeout“:** ufw blockt Anfragen aus dem Docker-Netz an Port 11434:
  `sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp`.
* Danach `docker compose restart`. Prüfen: `docker compose exec nuorb curl -sI https://huggingface.co | head -1`.

Die Ports 8443/8444 bleiben dabei nur im Heimnetz erreichbar, solange du sie am Router nicht freigibst.

## Für Entwickler

* `docker/src/` ist eine bereinigte Kopie der NuOrb-Oberfläche, des NuOrb-Admin-Panels und des STT-Routers
  (`python3 docker/tools/prepare-src.py`; bricht ab, wenn private Werte übrig bleiben).
* `app/` lädt die Originalserver unverändert als Modul und ergänzt Kopplung, s6-Dienste, Einrichtung.
* Image bauen: `docker compose build`. Veröffentlichung per GitHub Actions bei Tag `v*` (`github/workflows/docker.yml`).
* Test ohne GPU: `python3 docker/tools/mock-llm.py &`, dann
  `docker compose -p nuorb-test -f compose.yaml -f compose.test.yaml --env-file .env.test up -d --build` und
  `python3 docker/tools/e2e-test.py --setup-code <code>`.

---

## English summary

The NuOrb server runs as one Docker container (amd64/arm64): Hermes Agent, the NuOrb web UI, admin panel, local speech
recognition (Parakeet, CPU) and speech synthesis (Piper). The LLM is your own Ollama (≥ 64k context required), the
Claude API, or both with automatic fallback.

1. `git clone https://github.com/Anarki-LNX/nuorb && cd nuorb/docker`
2. `cp .env.example .env`: set `NUORB_HOSTS` (server IP), `NUORB_LANG=en`, `TZ`
3. `docker compose up -d` (first start downloads ~530 MB of speech models)
4. `docker compose logs nuorb | grep -i "setup code"`
5. Open `https://<server-ip>:8444`, enter the code, set a password, follow the setup (language, brain), then
   “Pair device” → scan the QR code with the NuOrb app or enter the code at `https://<server-ip>:8443`.

**Upgrading from v0.6.x or older:** from v0.7.0 on, volumes, paths, environment variables and services are all named
NuOrb. The migration script attached to release v0.7.0 moves an existing installation over. It makes a backup first,
keeps the old data and takes over password, memory and paired devices. Browsers have to pair once more.

**Tools (from v0.7.1):** the language model gets only what a voice assistant needs (web search, web pages, images,
memory, reminders, skills, NuOrb tools). Shell, Python, files, browser and sub-agents are off, also after an update.
Switch them on under “Tools” in the admin panel if you need them. After the assistant has read outside content, every
action with an effect needs a confirmation card, and shell, Python and browser cannot reach your home network.

**Fail-closed tool rules (from v0.7.4):** tools only run while NuOrb's rules are demonstrably active. If Hermes cannot
read `config.yaml` or NuOrb's hooks are missing, the gateway does not start (healthcheck unhealthy, clear log message).
If the hook is not active in a running Hermes, the `nuorb_guard` plugin blocks every tool call. An error during the
check blocks the call instead of letting it through (a small patch to Hermes, disclosed in `patches/` and in the image
label `org.nuorb.hermes.modified`). If the hook cannot read its rules, actions with an effect need a confirmation.

**Separate users (from v0.7.4):** Hermes and all tools of the language model run as `hermes` without capabilities, the
admin panel and the UI as their own user `nuorb`. NuOrb's secrets (admin password, sessions, internal keys, CA and
server keys, paired devices) belong to `nuorb` only, so an injected instruction cannot read them even with the shell
switched on. The UI keeps one read only capability, because Hermes sets its data folder to 0700 and the UI reads
`config.yaml`, `.env` and the memory there: it can read Hermes' files but not change them. Existing installations switch
over automatically at start.

**Update check (from v0.7.3):** every admin page shows the server version with a button “Check for updates”. The panel
asks github.com for the newest NuOrb release, at most every 6 hours and only while the admin panel is open. Switch the
automatic check off under “About”, or completely with `NUORB_UPDATE_CHECK=0`.

**Smart home (optional):** off by default. Enter Home Assistant / ioBroker access under “Smart Home” in the admin panel (or put
`/opt/data/smarthome.env` in place and set `NUORB_SMARTHOME=1`). Reading needs no confirmation, switching always shows a
confirmation card. Reliable with Claude; with a local model it is limited (turn off `tools.tool_search`).

**Troubleshooting:** enter the IP of the computer running Ollama, even if it is the same PC (start Ollama with `OLLAMA_HOST=0.0.0.0`); `.local` names often don't resolve inside the container.
No network inside the container (setup says “no internet connection”, often ufw with `DEFAULT_FORWARD_POLICY="DROP"`):
`sudo ufw default allow routed && sudo ufw reload`, then `docker compose restart`. Remove `"iptables": false` /
`"bridge": "none"` from `/etc/docker/daemon.json` if present (`sudo systemctl restart docker`). Ollama on the same PC
times out: `sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp`.

Never expose the ports to the internet; use a VPN. The QR code carries the fingerprint of the server's own CA, so the
app trusts exactly this server.
