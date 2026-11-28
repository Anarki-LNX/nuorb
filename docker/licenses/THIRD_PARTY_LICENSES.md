# NuOrb: third-party components and licenses

NuOrb's own code is under the [MIT license](LICENSE). NuOrb builds on many open-source projects and models; each keeps
its own license. This file lists them for the repository and for the Docker image `ghcr.io/anarki-lnx/nuorb`.

Inside the image this file, the license texts and the full Python package list are in **`/usr/share/doc/nuorb/`**:

```bash
docker compose exec nuorb ls /usr/share/doc/nuorb
docker compose exec nuorb cat /usr/share/doc/nuorb/python-packages.tsv
```

Checked for NuOrb 0.7.0 (packages and base image unchanged since the inventory of image 0.6.2, base `nousresearch/hermes-agent:v0.23.4`). This is a best-effort inventory, not
legal advice. Corrections are welcome as an issue.

## 1. Components that come with obligations

| Component | Version | License | What it means for you |
|---|---|---|---|
| [Piper](https://github.com/OHF-Voice/piper1-gpl) (`piper-tts`, text-to-speech engine), embeds [eSpeak NG](https://github.com/espeak-ng/espeak-ng) for phonemes | 1.8.0 (eSpeak NG 1.52.0.1) | **GPL-3.0-or-later** | Free to use, also commercially. If you **pass on** the image or a modified version, the GPL applies to Piper and to the parts that run with it in one process (the NuOrb web UI). NuOrb's code is MIT, which is GPL-compatible, and its source is public. Source of Piper: tag `v1.8.0` of piper1-gpl; eSpeak NG: release 1.52 on GitHub. |
| [FFmpeg](https://ffmpeg.org) static build by [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (part of the Hermes base image, `/opt/hermes/tools/ffmpeg-*`) | 9.0.1 (build 2026-08-31, `gpl` variant, no `nonfree` parts) | **GPL-3.0-or-later** | Same as above. Source: FFmpeg git tag `n9.0.1` plus the BtbN build scripts. NuOrb itself does not call FFmpeg. |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) (mDNS announcement) | 0.154.0 | LGPL-2.1-or-later | Unmodified, loaded as a separate Python package; you may replace it. |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) (Hermes) | 22.8 | LGPL-3.0-only | Unmodified, part of Hermes. |
| Debian GNU/Linux 13 base system (277 packages, e.g. bash, coreutils, Python 3.13, OpenSSL) | trixie | mostly GPL/LGPL/BSD/MIT, see `/usr/share/doc/<package>/copyright` | Sources: <https://snapshot.debian.org> (exact package versions: `dpkg -l` in the container). |
| [NVIDIA Parakeet-TDT-0.6B-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), int8 conversion by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (speech recognition, **downloaded at first start**, not in the image) | v3 | **CC-BY-4.0** | Commercial use allowed. Attribution: "Speech recognition: NVIDIA Parakeet-TDT-0.6B-v3 (CC-BY-4.0), ONNX export by k2-fsa/sherpa-onnx", shown in the admin panel under **Docker → About**. |
| webgl-noise by Ashima Arts and Stefan Gustavson (simplex noise in `ui/static/orb.js`) | 2011-2016 | MIT | Copyright and license notice in the header of `orb.js` and in [`webgl-noise-LICENSE.txt`](docker/licenses/webgl-noise-LICENSE.txt). |
| Fonts [Inter](https://github.com/rsms/inter), [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono), [Space Grotesk](https://github.com/floriankarsten/space-grotesk) | · | SIL OFL 1.1 | License texts next to the font files (`ui/static/fonts/LICENSE-*.txt`). |

## 2. Voices (Piper, downloaded at runtime)

NuOrb downloads the selected voice from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) into your
models volume. The voice files are not in the image; their license comes from the recordings they were trained on and
from the voice they were fine-tuned from (see each voice's `MODEL_CARD`).

| Voice | Offered as | Training data (license) | Fine-tuned from | Use |
|---|---|---|---|---|
| `de_DE-thorsten-medium` | **German default** | [Thorsten-Voice](https://www.thorsten-voice.de) by Thorsten Müller (CC0) | rdh (CC0) | free, also commercially |
| `en_US-joe-medium` | **English default** | [OHF-Voice/voice-datasets](https://github.com/OHF-Voice/voice-datasets) (CC0) | rdh (CC0) | free, also commercially |

NuOrb offers only voices whose recordings **and** base voice are CC0 or CC-BY. Up to version 0.6.2 it also offered
`en_GB-alan-medium` (English default), `en_US-ryan-high`, `en_US-amy-medium`, `de_DE-kerstin-low` and
`de_DE-thorsten-high`. They are built on the lessac voice (Blizzard Challenge 2013 data, research licence only) or on
RyanSpeech (CC-BY-NC-SA 4.0), or their license cannot be verified. Since 0.6.3 NuOrb no longer offers or downloads them;
installations that used one switch automatically to Joe (English) or Thorsten (German) and the old voice files are
deleted from the models volume.

Audio that NuOrb speaks is generated on your machine; the voice license decides what you may do with it. The admin panel shows the active voice with its license under **Docker → About**.

## 3. Everything else (permissive licenses)

### NuOrb's own Python environment (`/opt/nuorb/venv`, 36 packages)

All permissive except the ones listed in section 1: annotated-doc, annotated-types, anyio, h11, ifaddr, onnxruntime
1.32.0, fastapi, pathvalidate, pip, pydantic, pydantic_core, PyYAML, ruamel.yaml, typing-inspection (MIT);
cffi (MIT-0); click, httpcore, httpx, idna, protobuf, pycparser, starlette, uvicorn (BSD-3-Clause); segno (BSD);
cryptography (Apache-2.0 OR BSD-3-Clause); packaging (Apache-2.0 OR BSD-2-Clause); flatbuffers, opentelemetry-api,
python-multipart, sherpa-onnx 2.0.3 (Apache-2.0); numpy (BSD-3-Clause and others); certifi (MPL-2.0);
typing_extensions (PSF-2.0).

Piper ships two small helper models for Hebrew and Arabic diacritics (`piper/hebrew/nakdimon.onnx`, Nakdimon by
Elazar Gershuni, MIT; `piper/tashkeel/model.onnx`, libtashkeel, MIT). NuOrb does not use them.

### Hermes Agent base image (`/opt/hermes`)

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research, v0.23.4: **MIT** (`/opt/hermes/LICENSE`).
  **Modified in this image (since NuOrb 0.7.4):**
  - `agent/tool_executor.py`, function `_pre_tool_block`: an error in the `pre_tool_call` hook dispatch blocks the tool call instead of letting it run. Patch with explanation: `/usr/share/doc/nuorb/patches/hermes_hook_fail_closed.py`, in the repository `docker/patches/`.
  - Added `/opt/hermes/plugins/nuorb_guard` (NuOrb, MIT): blocks tool calls while NuOrb's policy hook is not registered.
  Everything else in `/opt/hermes` is unchanged. The image label `org.nuorb.hermes.modified` says the same.
- Its Python environment (158 packages): permissive (MIT, BSD, Apache-2.0, ISC, PSF), plus MPL-2.0 (certifi, mautrix,
  pathspec, unmodified) and LGPL-3.0 (python-telegram-bot, section 1). `fal_client` and `agent-client-protocol` are
  Apache-2.0, `resvg_py` is MIT (license files in the packages).
- Node.js 26.12.1 (MIT and bundled licenses), npm 12.2.2 (Artistic-2.0), 330 npm packages (ISC, MIT, Apache-2.0,
  BSD, BlueOak-1.0.0, CC0; `spdx-exceptions` CC-BY-3.0); each keeps its license file in `node_modules`.
- Python 3.14.9 (PSF-2.0), ripgrep 15.2.0 (MIT or Unlicense), uv 0.13.5 (MIT or Apache-2.0).
- [s6-overlay](https://github.com/just-containers/s6-overlay) 3.2.3.0 with s6, s6-rc, execline (ISC), tini (MIT).
- `tools/wakewords/hey_hermes.tflite`: Hermes' own wake-word model; Hermes states it may be redistributed. NuOrb does
  not use it (the engine is not installed).

## 4. Telemetry

onnxruntime 1.32 (PyPI build) contains Microsoft's 1DS telemetry client and, unless switched off, sends usage events
to `mobile.events.data.microsoft.com`. The NuOrb image sets `ORT_DISABLE_TELEMETRY=1` for every process (image `ENV`)
and the web UI sets it again before loading Piper. Measured offline with `docker/tools/telemetry-check.sh`: with the
image defaults there are no connection attempts; with the switch removed there are several within 45 seconds.

## 5. Brains you connect yourself

Ollama, llama.cpp, vLLM, LM Studio, LocalAI and the language models you run with them are **not** part of NuOrb.
Their licenses (e.g. Gemma Terms of Use, Llama Community License, Qwen/Apache-2.0) apply to you directly. The same
goes for the Anthropic API (Claude): its terms apply when you enter your own key.

## 6. Phone app and wake word

The NuOrb Android app and its "Hey NuOrb" wake-word models are not part of the server image; the app documents its
own licenses. The NuOrb server only passes wake-word files to the app if the operator puts them into
`/opt/data/nuorb/wakeword/`.

## 8. Weather data (built-in weather)

NuOrb's built-in weather tool asks the weather services directly from **your** NuOrb server (your IP address), there is
no central NuOrb service in between. Your place is stored only on your server (`/opt/data/nuorb/config/weather.json`),
requests carry only coordinates rounded to about one kilometre and a neutral `User-Agent`, and answers are cached on
your server (measurements and warnings 10 min, forecasts 60 min) to go easy on the services. Checked for
NuOrb 0.7.x on 2026-11-28. This is a best-effort summary, not legal advice.

| Source | Used for | License / terms | What NuOrb does about it |
|---|---|---|---|
| [Deutscher Wetterdienst](https://www.dwd.de) (DWD) open data | Germany: measurements of the nearest DWD station, MOSMIX forecast, official warnings | [CC BY 4.0](https://www.dwd.de/EN/service/legal_notice/legal_notice.html), source notice required ("Quelle: Deutscher Wetterdienst"). If an official warning is changed, the source notice must be removed. | Every spoken answer names the source ("laut Deutschem Wetterdienst" / "according to the German Weather Service"). Official warnings are passed on unchanged (DWD headline and validity), never rephrased. Notice "Quelle: Deutscher Wetterdienst" in the admin panel under Weather and under About. |
| [Bright Sky](https://brightsky.dev) (`api.brightsky.dev`) | Access to the DWD data as JSON, no key | Code MIT. The data stays DWD data under the DWD terms. The public instance is "free-to-use for all purposes" and run by a volunteer. | Cache and a clear `User-Agent`. You can enter your own Bright Sky instance in the admin panel. |
| [Open-Meteo](https://open-meteo.com) | Places outside Germany: current weather and forecast | Data [CC BY 4.0](https://open-meteo.com/en/licence) ("Weather data by Open-Meteo.com"). The free API is for **non-commercial use only** (personal home automation counts as non-commercial), fewer than 10 000 calls per day per IP. | Spoken answers name "Open-Meteo", link in the admin panel. For commercial use enter your own Open-Meteo API key in the admin panel. NuOrb then uses the customer API. |
| [Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api) with [GeoNames](https://www.geonames.org) | Finding a place by its name, once at setup (no postcode search) | GeoNames data CC BY 4.0 ("Location data based on GeoNames") | Notice in the admin panel next to the search. |

## Trademarks

NuOrb is not affiliated with Nous Research, NVIDIA, Anthropic, Google, Home Assistant, ioBroker, GitHub or any other
project named here. All names and trademarks belong to their owners and are used only to describe compatibility.
