# NuOrb: notices

NuOrb's own code is under the MIT license, see [LICENSE](LICENSE).

Third-party material in this repository keeps its own license:

- `ui/static/fonts/*.woff2`: Inter, JetBrains Mono, Space Grotesk, all under the SIL Open Font License 1.1 (see `ui/static/fonts/LICENSE-*.txt`).
- `ui/static/orb.js`: the simplex noise function is from webgl-noise, Copyright (C) 2011 Ashima Arts, (C) 2011-2016 Stefan Gustavson, MIT license (see `docker/licenses/webgl-noise-LICENSE.txt`).
- `voice/saetze.tsv`: sentences from Mozilla Common Voice (German sentence collector and Wikipedia extracts), CC0 1.0.

Modified third-party code in the Docker image (since NuOrb 0.7.4):

- Hermes Agent (Nous Research, MIT), file `agent/tool_executor.py`, function `_pre_tool_block`: an error in the `pre_tool_call` hook dispatch blocks the tool call instead of letting it run, so NuOrb's tool rules cannot be skipped silently. The patch and its explanation are in [`docker/patches/hermes_hook_fail_closed.py`](docker/patches/hermes_hook_fail_closed.py) and in the image under `/usr/share/doc/nuorb/patches/`. The image build stops if the patched code is not found exactly once. NuOrb also adds its own plugin `/opt/hermes/plugins/nuorb_guard` (MIT). The image label `org.nuorb.hermes.modified` names both.

All third-party components of the Docker image, the Proxmox LXC template and the models NuOrb downloads are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
