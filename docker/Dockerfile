# NuOrb-Server – selbst gehosteter Sprachassistent als ein Container.
# Basis: offizielles Hermes-Agent-Image (s6-überwacht, Benutzer hermes, Daten unter /opt/data).
# Darauf: NuOrb-Oberfläche (:8443), Admin-Panel (:8444), STT-Router mit Parakeet (CPU) und Piper-TTS.
# Modelle und Stimmen werden beim ersten Start ins Volume /opt/models geladen, nicht ins Image gebacken.
# Basis-Images per Digest festgelegt (Tag zur Lesbarkeit, Digest zählt). Dependabot schlägt Updates vor.
ARG HERMES_IMAGE=nousresearch/hermes-agent:v0.23.4@sha256:ef0650d523ee1b5ab32c779acabc446d9936869a9932591553688a62bcf0c42f

FROM busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec AS files
COPY src/ui/ /f/opt/nuorb/ui/
COPY src/admin/ /f/opt/nuorb/admin/
COPY src/stt/ /f/opt/nuorb/stt/
COPY src/smarthome/ /f/opt/nuorb/smarthome/
COPY app/ /f/opt/nuorb/app/
COPY rootfs/ /f/
COPY licenses/ /f/usr/share/doc/nuorb/
COPY patches/ /f/usr/share/doc/nuorb/patches/
RUN mkdir -p /f/opt/nuorb/home/.config /f/opt/models

FROM ${HERMES_IMAGE}

USER root
# Security updates of the Debian base packages (the image scan reports them as fixed upstream).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
# Schmaler Patch an Hermes: ein Fehler der Hook-Verteilung blockiert den Werkzeugaufruf (statt ihn durchzulassen).
# Bricht den Build ab, wenn die Stelle im Hermes-Code nicht genau einmal gefunden wird. Siehe patches/.
COPY patches/hermes_hook_fail_closed.py /tmp/hermes_hook_fail_closed.py
RUN /opt/hermes/.venv/bin/python /tmp/hermes_hook_fail_closed.py /opt/hermes/agent/tool_executor.py \
 && /opt/hermes/.venv/bin/python -m py_compile /opt/hermes/agent/tool_executor.py \
 && rm /tmp/hermes_hook_fail_closed.py
COPY requirements.txt /opt/nuorb/requirements.txt
# Eigenes venv mit Debians Python 3.13 (Hermes nutzt sein eigenes 3.14-venv, das bleibt unberührt).
# requirements.txt ist aus requirements.in erzeugt und enthält jedes Paket mit Hash: pip bricht ab, wenn eine Datei abweicht.
# v0.7.4: eigener Benutzer für Admin-Panel und Oberfläche, getrennt von hermes (Werkzeuge des Sprachmodells)
RUN groupadd --system --gid 10010 nuorb \
 && useradd --system --uid 10010 --gid nuorb --groups hermes --home-dir /opt/nuorb/home --no-create-home \
      --shell /usr/sbin/nologin nuorb
RUN /usr/bin/python3 -m venv /opt/nuorb/venv \
 && /opt/nuorb/venv/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps -r /opt/nuorb/requirements.txt \
 && find /opt/nuorb/venv -name '__pycache__' -prune -exec rm -rf {} + \
 && rm -rf /root/.cache

# Bereinigte Quellen (docker/tools/prepare-src.py) + Docker-Hülle + s6-Dienste: in einer Zwischenstufe
# zusammenstellen und mit EINEM COPY übernehmen (wenige Schichten, wichtig bei Storage-Treibern wie vfs)
COPY --from=files /f/ /
RUN ln -sfn /opt/data /opt/nuorb/home/.hermes \
 && ln -sfn /opt/data/nuorb/config /opt/nuorb/home/.config/nuorb \
 && chown hermes:hermes /opt/models \
 && /opt/nuorb/venv/bin/python -m compileall -q /opt/nuorb/app /opt/nuorb/ui /opt/nuorb/admin /opt/nuorb/stt /opt/nuorb/smarthome \
 && /opt/nuorb/venv/bin/python -I /opt/nuorb/app/license_inventory.py /usr/share/doc/nuorb
# Lizenzen: /usr/share/doc/nuorb/ (THIRD_PARTY_LICENSES.md, LICENSE-NuOrb.txt, Python-Paketliste + Lizenzdateien);
# Debian-Pakete: /usr/share/doc/<paket>/copyright, Lizenztexte: /usr/share/common-licenses/
LABEL org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later AND LGPL-2.1-or-later AND LGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/Anarki-LNX/nuorb" \
      org.opencontainers.image.title="NuOrb" \
      org.nuorb.hermes.modified="agent/tool_executor.py _pre_tool_block: an error in the pre_tool_call hook dispatch blocks the tool call (patch /usr/share/doc/nuorb/patches/hermes_hook_fail_closed.py); added plugin /opt/hermes/plugins/nuorb_guard"

EXPOSE 8443 8444
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 CMD ["/opt/nuorb/bin/healthcheck"]

# Hermes-Gateway (Agent + API auf 127.0.0.1:8642) als Hauptprozess; die NuOrb-Dienste laufen daneben unter s6.
# Version für die Anzeige im Admin („NuOrb Server v…“); im Release setzt der GitHub-Workflow den Tag ein
# Keine Telemetrie: onnxruntime (pip, ab 1.32) sendet sonst Nutzungsdaten an Microsoft (1DS, OneCollector).
# Nur die Umgebungsvariable wirkt (disable_telemetry_events() allein nicht, gemessen); ENV gilt für jeden Prozess.
ENV ORT_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
ARG NUORB_BUILD=dev
ENV NUORB_BUILD=${NUORB_BUILD}
# Vor dem Start des Gateways: config.yaml lesbar, NuOrbs Policy-Hooks drin (sonst kein Start, siehe app/hermes_guard.py)
ENV PATH=/opt/nuorb/guard:/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CMD ["gateway", "run"]
