# Room Agent: Raspberry Pi hub and Windows companion

The room agent supports the tested Pi-to-notebook checkpoint and an experimental
standalone Pi-to-TV path:

```text
ReSpeaker -> Raspberry Pi capture -> Pi whisper.cpp -> deterministic router -> typed action -> notebook companion -> YouTube
                                                       \-> Hermes tools -> OpenRouter -> typed action
                                                       \-> direct OpenRouter -> local Qwen fallbacks
ReSpeaker -> Raspberry Pi capture -> Pi whisper.cpp -> typed action -> local Chromium -> HDMI TV
                                                                    \-> Room Canvas -> HDMI TV
```

The protocol is reusable; `web.open` is merely its first registered capability. The Windows companion never accepts arbitrary URLs, shell commands, keystrokes, or mouse coordinates. A local model may propose only the small typed action schema, which Python validates before execution.

The YouTube capabilities resolve either the exact configured Cinkátko channel or a validated free-form podcast title. They do not automate screen coordinates: the companion uses the official API to identify the video, validates its ID and opens the exact watch page.

## 1. Configure the YouTube lookup on Windows

In Google Cloud, enable **YouTube Data API v3** and create an API key restricted to that API. Then, from this folder:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `YOUTUBE_API_KEY` in `.env`. Keep the fixed `@mitoncz` handle and the
600-second minimum unless the product definition changes. The `.env` file is
excluded from Git and must never be committed. For notebook mode it stays on
Windows; for standalone TV mode a protected copy is stored locally on the Pi.

Validate the live lookup without opening a browser:

```powershell
python windows_companion.py --check-youtube
```

It should print the selected title, publication time, duration and exact watch URL.

The exact Cinkátko resolver uses three list operations: it resolves the configured
handle to the channel's uploads playlist, reads the newest uploads and requests
their duration/status metadata. Free-form podcast lookup first searches for
matching podcast channels, requires a textual connection in the title or
description, recognizes host names in channel metadata, and uses audience size
only as a secondary signal. It refuses a low-confidence match rather than playing
an unrelated popular show, then reads the selected channel's uploads playlist.
A newer Short, private video, unembeddable video, live broadcast or item below ten
minutes is skipped.

Verified or ambiguous show identities live in `podcasts.json`, not in routing
code. An explicit request for “Hard Fork” or “Platformer” uses its pinned channel
ID. “Casey Newton podcast” deliberately returns both choices and asks for the
podcast title because Casey hosts both shows. New catalog entries can add aliases
without adding another spoken-command branch; uncatalogued names continue through
confidence-scored discovery.

## Hybrid semantic router

Known commands continue through the deterministic router without invoking a model.
This includes common requests such as `Play the latest Lex Fridman podcast`: the
router extracts the spoken show name and sends it through the same validated,
confidence-scored YouTube discovery used for every uncatalogued podcast. It does
not require a title-specific command branch. When no deterministic action matches,
the Pi uses OpenRouter when `OPENROUTER_API_KEY` is configured, then falls back to
the loopback-only llama.cpp server if the cloud request fails. Both providers may
return only `action`, `target` and `query`; Python validates and converts that
proposal into the existing typed protocol. Playback controls always use the tracked
active device rather than trusting a model-selected target.

OpenRouter calls the fixed `https://openrouter.ai/api/v1/chat/completions` endpoint
with Bearer authentication. The model slug is configurable, but cannot replace the
endpoint. The request requires strict JSON Schema support and rejects extra fields
before any device action is considered. The default focused intent model is
`deepseek/deepseek-v4-flash`; change `OPENROUTER_MODEL` to A/B-test another
supported model. Extended reasoning is disabled for these short classification
requests to keep latency and token usage low.

Put the secret only in the Pi's ignored `.env` file:

```dotenv
OPENROUTER_API_KEY=your-key-here
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
OPENROUTER_TIMEOUT_SECONDS=4.0
```

Then protect it with `chmod 600 /home/voiceagent/room-agent/.env`. Never add the
real key to `.env.example`, terminal screenshots, logs or Git. Known commands do
not call a model. A semantic command prints `SEMANTIC: hermes`,
`SEMANTIC: openrouter` or `SEMANTIC: local`, making the selected path visible
during testing.

The tested Pi 5 configuration uses:

- Qwen3.5-0.8B Q4_K_M at
  `/home/voiceagent/models/Qwen3.5-0.8B-Q4_K_M.gguf`;
- the official llama.cpp ARM64 runtime at
  `/home/voiceagent/llama-runtime-b10280`;
- a 1,024-token context, four threads and weight repacking disabled;
- `http://127.0.0.1:8767`, which is not exposed to the LAN.

Downloaded models and runtime binaries do not belong in Git. The included
`room-agent-llm.service` records the reproducible fallback configuration. If all
semantic providers are unavailable, deterministic commands still work; unmatched
natural language returns an error rather than executing an unvalidated action.

## Optional Hermes tool sidecar

Hermes Agent can sit between the deterministic parser and the existing semantic
fallbacks. This adds a bounded tool loop for requests that benefit from current
web research or podcast-name disambiguation. Hermes does **not** receive audio,
own the HDMI browser, or execute arbitrary commands: it receives only the local
Whisper transcript and must finish with the same three-field room-action schema.
Python still validates and executes the action. If Hermes is unavailable, the
hub falls back to direct OpenRouter and then the local Qwen model.

The supplied 4 GB Pi profile exposes only `search`, `browser`, and `memory` to the
Hermes API platform. Terminal, file editing, code execution, delegation and
computer control are absent. A hard six-turn ceiling, one concurrent request,
two-search cap, loop circuit-breaker, and systemd memory limit keep the sidecar
bounded. Browser automation starts only if a request genuinely needs it; it is
not a second permanent Chromium window on the TV.

### Install and configure Hermes on the Pi

Install the official Linux ARM64 build while logged in as `voiceagent`:

```bash
curl --proto '=https' --tlsv1.2 -fL \
  https://hermes-agent.nousresearch.com/install.sh \
  -o /tmp/hermes-install.sh
less /tmp/hermes-install.sh
bash /tmp/hermes-install.sh --skip-setup
rm /tmp/hermes-install.sh
```

Review the downloaded installer before running it. Do not pipe a mutable network
response directly into a shell.

Run the included configurator. It reads the already-protected OpenRouter key from
the room agent, generates a separate 256-bit sidecar secret, writes matching
client/server values without printing either secret, installs the restricted
profile and both user units, and backs up a pre-existing Hermes config before
replacing it:

```bash
cd /home/voiceagent/room-agent
python3 configure_hermes.py
```

It expects `OPENROUTER_API_KEY` to already exist in
`/home/voiceagent/room-agent/.env`. It creates these matching private values:

- `API_SERVER_KEY` in `/home/voiceagent/.hermes/.env`;
- `HERMES_API_KEY` in `/home/voiceagent/room-agent/.env`.

It also enables the loopback client settings in the room-agent environment:

```dotenv
HERMES_API_URL=http://127.0.0.1:8642
HERMES_API_MODEL=hermes-agent
HERMES_TIMEOUT_SECONDS=25.0
```

The supplied profile uses Hermes's key-free DDGS provider for search, so no
additional search account is required. DDGS is search-only; the browser tool can
inspect an interactive result page, while Firecrawl, Tavily, Exa, or another
extract-capable backend can be added later for deeper document reading. Ordinary
playback continues through the room agent's existing restricted Chromium profile.

Install and start the loopback-only user service:

```bash
mkdir -p ~/.config/systemd/user
cp /home/voiceagent/room-agent/room-agent-hermes.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now room-agent-hermes.service
systemctl --user restart room-agent.service
```

Verify the authenticated API without revealing the key:

```bash
set -a
. /home/voiceagent/.hermes/.env
set +a
curl -fsS -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/v1/models
```

For an end-to-end typed rehearsal, use a natural phrase that is broader than a
fixed command and check for `SEMANTIC: hermes`:

```bash
cd /home/voiceagent/room-agent
python3 pi_hub.py --text "Figure out which Y Combinator podcast release is newest and put it on the big screen" --no-led
```

## Standalone Pi-to-TV checkpoint

This is the smallest boss-demo configuration and does not require the notebook
or Google TV Streamer:

```text
ReSpeaker -> Raspberry Pi 5 -> HDMI -> TV
```

Copy the restricted `.env` file to `/home/voiceagent/room-agent/.env` and protect
it with `chmod 600`. The Pi must have an active desktop on its HDMI output and
Chromium installed. Test the complete local route without microphone audio:

```bash
cd /home/voiceagent/room-agent
python3 pi_hub.py --text "Play the latest Cinkatko podcast on the TV"
python3 pi_hub.py --text "Play the latest Lex Fridman podcast on the TV"
```

When microphone testing is inconvenient, use the persistent text console to
rehearse a complete multi-command session without restarting the hub:

```bash
python3 pi_hub.py --text-console
```

Type the same commands that would normally be spoken. The console preserves the
active browser and media context, so sequences such as `Google Miton`, `Open the
first one`, and `Close tab` exercise the same routing and execution path after
speech recognition. Use `:status` to inspect the active target, `:help` for the
console controls, and `:exit` to leave without turning the word `quit` into a
console shortcut.

The Pi resolves the allowlisted episode itself and opens the exact watch page in
a dedicated Chromium kiosk profile at `~/.config/room-agent/chromium`. The
included allowlisted kiosk helper accepts YouTube's one-time consent screen and
starts the video without a mouse. It is restricted to YouTube and Google consent
pages and does not interact with arbitrary sites. The notebook remains an
optional fallback rather than a runtime dependency.

In standalone mode, the short command “Open YouTube” opens the allowlisted
YouTube homepage on the HDMI display. If `--pc-url` is configured, the same
unqualified command keeps the notebook as its default; saying “on the TV” or
“on the laptop” always selects that explicit target.

The same typed website action supports “Open miton.cz” using the fixed canonical
`https://www.miton.cz/` allowlist entry. No transcript text is used as a URL.

“Google Miton” uses a separate `web.search` action. Only the query text is
variable; Python validates and URL-encodes it into the fixed Google Search origin
before launching the browser.

General informational questions use the same capability. For example, “What's
the weight of an elephant?”, “Who founded Miton?” and “How tall is the Eiffel
Tower?” open a Google results page for the complete validated question. This is
a generic question pattern rather than a list of topic-specific answers. Add “on
the laptop” or “on the TV” to select a device explicitly.

After a Google search, “Open the first page” or “Open the first result” reuses
the remembered query. The restricted kiosk helper accepts Google's consent page
and opens the first organic result without screen coordinates or an arbitrary URL
supplied by the model.

### Capability-aware smart displays

The `tv` target is no longer assumed to mean the Pi desktop. At startup the hub
loads the private `display_endpoints.json` enrollment file and actively probes
each trusted endpoint. Selection happens independently for every capability:

| Request | Preferred native capability | Fallback |
|---|---|---|
| Open an installed app | Exact discovered/enrolled app | None for unapproved web apps |
| Open YouTube | Native YouTube app | Pi HDMI Chromium |
| Play an exact YouTube episode | Native YouTube deep link | Pi HDMI Chromium |
| Google a question | Verified native browser | Pi HDMI Chromium |
| Pause, continue, close | Endpoint that opened the active session | Never jumps screens |

This does **not** control every smart device discovered on a corporate network.
Only explicitly enrolled and previously paired endpoints are candidates. HDMI
EDID can identify a panel model but cannot prove that its apps have a remote API,
and network discovery cannot determine which of several conference-room TVs is
physically in front of the user. Enrollment supplies that room association;
the live probe supplies reachability and exact capabilities.

The native adapters are:

- **Google/Android TV:** verifies its paired ADB session, installed app packages,
  a resolvable browser activity and exact YouTube deep links. The Android app
  registry is data in `display_endpoints.json`, so adding an installed app does
  not require a new voice-command script.
- **Apple TV:** asks pyatv for the actual launchable app list through the existing
  Companion pairing. Apple TV has no general web browser, so Google searches use
  HDMI fallback.
- **LG webOS:** discovers the TV through its second-screen SSDP service, stores a
  one-time pairing key outside the repository, reads the installed app list and
  exposes native app launch, YouTube content IDs, media controls and the LG
  browser only when each capability is present.
- **Samsung Tizen:** pairs through the modern local WebSocket remote, stores its
  token outside the repository, reads the installed app list, launches native
  apps and sends explicit play, pause and Home keys. The browser and exact
  YouTube playback are advertised only when that TV reports its browser app.
- **Pi HDMI:** remains the final browser/YouTube fallback. If the Pi is headless
  and no native device exposes the requested capability, the command fails
  explicitly instead of pretending that it ran.

When any enrolled native endpoint publishes executable capabilities, Room Canvas
is not opened automatically, so the TV stays on its native interface. Media
controls retain endpoint affinity: a failed Apple TV or Google TV pause never
pauses an unrelated Chromium session.

#### Enroll a Google/Android TV

Install ADB on the Pi:

```bash
sudo apt update
sudo apt install -y adb
```

On Android TV 13 or newer, enable Developer options and Wireless debugging as
described in the official [ADB wireless debugging guide](https://developer.android.com/tools/adb#connect-to-a-device-over-wi-fi).
Use the pairing address/code shown by the TV, then connect with the separate
connection address shown on the Wireless debugging page:

```bash
adb pair TV_IP:PAIRING_PORT
adb connect TV_IP:CONNECTION_PORT
adb devices -l
```

The pairing persists until it is revoked on the TV. Enroll the connected serial
printed by `adb devices -l`:

```bash
cd ~/room-agent
python3 configure_display.py android \
  --name "Google TV Streamer" \
  --serial TV_IP:CONNECTION_PORT \
  --host TV_IP
```

If Android assigns a different wireless-debugging connection port after a
reboot, the adapter matches the enrolled host against ADB's paired
`_adb-tls-connect._tcp` mDNS service and reconnects to the new port. Corporate
Wi-Fi client isolation can block this discovery and must be tested in the real
room.

The default Android registry probes native YouTube and Netflix packages. Add
further `app name -> package` entries to the private JSON only after confirming
the installed package with ADB.

#### Enroll the already-paired Apple TV

The existing pyatv pairing remains in pyatv's private storage. Its Companion
[Apps interface](https://pyatv.dev/development/apps/) lists and launches the
apps actually installed on that Apple TV. Enroll only its non-secret address
and identifier:

```bash
cd ~/room-agent
python3 configure_display.py apple \
  --name "Conference Room Apple TV" \
  --host APPLE_TV_IP \
  --identifier APPLE_TV_IDENTIFIER
```

#### Discover and pair an LG webOS TV

The LG control dependency is isolated in the existing project virtual
environment so the boot service can continue using Debian's system Python:

```bash
cd ~/room-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-pi.txt
```

Put the powered-on TV and Pi on the same local network, then stop the listener
for the one-time pairing. With one LG TV on the LAN, its address is discovered
automatically:

```bash
systemctl --user stop room-agent.service
cd ~/room-agent
python3 configure_display.py lg --name "Home LG TV"
```

Approve the **Room Agent** prompt on the TV. The pairing key is saved at
`~/.config/room-agent/lg-webos.sqlite` with user-only permissions; it is not
written into the enrollment JSON or repository. If multicast discovery is
blocked or multiple LG TVs are found, use the intended address explicitly:

```bash
python3 configure_display.py lg --name "Home LG TV" --host LG_TV_IP
```

Recent TVs use the secure webOS control port. If an older model is reachable but
pairing reports that port 3001 is unavailable, retry once with
`--without-ssl`; that explicitly selects the legacy local port 3000.

Pairing should survive Pi and TV restarts. It must be repeated only if the TV
revokes the connection, its network identity changes, or the local key file is
deleted. The adapter learns installed app identifiers from the TV rather than
assuming that every LG generation uses the same Netflix or YouTube ID.

Run the complete typed rehearsal in one process so pause, continue and close
retain the display session:

```bash
python3 pi_hub.py --display-status --no-led
python3 pi_hub.py --text-console --no-led
```

At the `room-agent>` prompt, try these one at a time:

```text
Open Netflix on the TV
Close
Open YouTube on the TV
Close
Play the latest Cinkatko podcast on the TV
Pause
Continue
Close
Google the weight of an elephant on the TV
:exit
```

The status output should name `Home LG TV` with kind `lg_webos`. Installed apps
open in the TV's native interface. Google uses the native LG browser only when
the browser is reported by that TV; otherwise it deliberately falls back to the
Pi HDMI browser. Restart hands-free mode after the rehearsal:

```bash
systemctl --user start room-agent.service
```

#### Pair a modern Samsung Tizen TV

Samsung uses the same isolated project environment as LG. Install the pinned
dependencies first, then put the powered-on TV and Pi on the same local network.
Stop the listener for the one-time pairing:

```bash
cd ~/room-agent
.venv/bin/python -m pip install -r requirements-pi.txt
systemctl --user stop room-agent.service

python3 configure_display.py samsung \
  --name "Home Samsung TV" \
  --host SAMSUNG_TV_IP
```

Approve the **Room Agent** prompt on the TV. Modern Tizen normally uses secure
port 8002 and saves its pairing token at
`~/.config/room-agent/samsung-token.txt`. The token is never copied into the
enrollment JSON or repository. A legacy model that exposes only unencrypted
local control can be tested explicitly with `--port 8001`; Room Agent never
silently downgrades the connection.

Verify the exact capabilities exposed by this model:

```bash
python3 pi_hub.py --display-status --no-led
python3 pi_hub.py --text-console --no-led
```

Try `Open Netflix on the TV`, `Pause`, `Continue`, and `Close`. App identifiers
come from the TV rather than a hard-coded Samsung catalog. If the installed-app
reply does not include a browser, Google and exact podcast playback stay on the
Pi HDMI fallback. The previously tested `UE55KU6072` timed out on both local
control ports, so this adapter intentionally does not claim that every older
Samsung model supports network control.

#### TV families, not logo-specific scripts

A brand name does not reliably identify the control protocol. For example, TCL
sells both Roku TV and Google TV models. The compatibility target is therefore
four native platform adapters plus one reusable Android profile:

| Platform family | Typical brands/products | Room Agent status |
|---|---|---|
| Google/Android TV | Google TV Streamer; many Sony, TCL, Philips, Hisense and Xiaomi models | Native adapter ready |
| LG webOS | LG TVs | Native adapter ready; hardware acceptance test pending |
| Samsung Tizen | Modern Samsung TVs | Native adapter ready; model capability probe required |
| Roku TV | Roku and some TCL/Hisense models | Planned official ECP adapter |
| Fire TV | Fire TV devices and Fire TV Edition panels | Planned profile over the Android/ADB adapter |

VIDAA, Titan OS, Saphi and older proprietary TV systems remain explicit HDMI
fallbacks until a model exposes a stable, testable local API. This is more honest
than claiming blanket support for a brand whose models use several unrelated
operating systems.

#### Inspect the decision before a demo

Stop the hands-free listener briefly and print the live capability report:

```bash
systemctl --user stop room-agent.service
cd ~/room-agent
python3 pi_hub.py --display-status --no-led
systemctl --user start room-agent.service
```

Typical capability names are `app.youtube`, `app.netflix`, `web.search`,
`youtube.play`, `media.pause`, `media.resume` and `media.close`. Try these typed
acceptance checks before voice testing:

```bash
python3 pi_hub.py --text "Open Netflix on the TV" --no-led
python3 pi_hub.py --text "Play the latest Cinkatko podcast on the TV" --no-led
python3 pi_hub.py --text "Google Miton on the TV" --no-led
```

### Room Canvas

`--room-canvas` starts a lightweight visual status screen on the HDMI output. It
shows the live READY/LISTENING/TRANSCRIBING/ROUTING/EXECUTING state, the latest
transcript, the validated action and target, the selected result, and measured
latency for each local processing stage. The interface is plain local HTML, CSS
and JavaScript served by a background thread in the existing hub process; it
does not add another Python service or send state off the Pi.

The server binds only to `127.0.0.1:8768`. Chromium loads the canvas at startup
and reuses the same dedicated kiosk profile for allowlisted media. Saying
`Close tab` terminates the media kiosk and immediately brings the canvas back.
The included user service enables this mode by default. For a manual rehearsal:

```bash
python3 pi_hub.py --text-console --room-canvas
```

The canvas is display-only: it cannot submit commands or turn arbitrary browser
content into actions. `--canvas-port` may change the loopback port when 8768 is
already occupied.

### iPhone Action Button push-to-talk

The preferred iPhone input is the native SwiftUI app in
[`ios/RoomAgentApp`](ios/RoomAgentApp). A one-use QR pairs the phone without
copying tokens or constructing JSON. The app stores its credential in Keychain,
publishes **Talk to Room Agent** as an App Shortcut, records one visible
push-to-talk request, and sends only the transcript through private Tailscale
HTTPS. The hand-built Apple Dictation Shortcut remains a supported fallback.
Requests carry idempotency UUIDs and are rate-limited and metadata-audited.

Follow [IPHONE_PRODUCTION.md](IPHONE_PRODUCTION.md) for deployment, the exact
Shortcut actions, token rotation/revocation, failure tests, and operations.

When the Windows notebook should be the orchestrator and display endpoint, use
[IPHONE_WINDOWS.md](IPHONE_WINDOWS.md). That path needs no Pi, runs one
background process at Windows login, and owns a dedicated Brave/Chrome/Edge session
so voice `Close` never touches unrelated browser tabs. It also retains the
active YouTube surface across commands: `Open YouTube` followed by
`Play <specific title or topic>` searches and plays on the same display.

For the connector-first smart-TV path, see
[USB_TV_CONNECTOR.md](USB_TV_CONNECTOR.md). It includes a signed virtual
ATOMS3U simulator, compile-checked USB/Wi-Fi diagnostic images and the production
Wi-Fi connector firmware with generic USB HID plus bounded Sony and LG infrared
profiles, including an LG webOS hybrid that combines IR remote keys with USB
search and text entry. The IR profiles can use either the built-in GPIO 12 emitter or a
prepared M5Stack Unit IR U002 build on Grove GPIO 2 without changing the host,
iPhone or voice commands. Bluetooth/NimBLE is excluded from that production
image because TinyUSB/Bluetooth coexistence has not been accepted. All LG
controls still require exact-model physical acceptance testing; the
connector guide records those tests and the external-emitter setup.

### Plug-and-speak startup

Hands-free mode waits for an utterance and trims leading/trailing room silence.
The initial allowlisted TV command starts playback. While the session is active,
media commands accept only short, complete control utterances so ordinary podcast
sentences are ignored rather than controlling themselves.

After playback starts, the hands-free controls are:

- `Pause.` or `Pause the video.`
- `Continue.` or `Continue the video.`
- `Quit.` or `Close tab.`

The close command terminates only Chromium's dedicated room-agent kiosk profile,
including its isolated process group when Chromium hands a newly opened tab from
its short-lived launcher to the existing kiosk process. It clears the active TV
session, returns to Room Canvas when enabled (or the Pi desktop otherwise), and
keeps the listener ready for the next play command. The short close phrases must match the complete utterance and work
only during an active TV session; this avoids treating an ordinary podcast
sentence containing "quit" or "close" as a command.

Install the room hub and Hermes user services while logged into the Pi desktop
account. Keep the Qwen service installed but stopped during the normal online
demo; this avoids permanently loading both cloud-agent and offline-model stacks
into the Pi's 4 GB RAM:

```bash
mkdir -p ~/.config/systemd/user
cp /home/voiceagent/room-agent/room-agent.service ~/.config/systemd/user/
cp /home/voiceagent/room-agent/room-agent-llm.service ~/.config/systemd/user/
cp /home/voiceagent/room-agent/room-agent-hermes.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now room-agent-hermes.service
systemctl --user disable --now room-agent-llm.service
systemctl --user enable --now room-agent.service
```

Before an intentionally offline rehearsal, stop Hermes and start the local Qwen
fallback. The deterministic command path works in either configuration:

```bash
systemctl --user stop room-agent-hermes.service
systemctl --user start room-agent-llm.service
```

After future boots, the directional listening LEDs indicate that the Pi is
armed. Say “Play the latest Cinkatko podcast on the TV” without opening SSH or
pressing Enter. During playback, say “Pause,” “Continue,” or “Quit.” Inspect the
service state only when troubleshooting:

```bash
systemctl --user status room-agent.service --no-pager
systemctl --user status room-agent-hermes.service --no-pager
systemctl --user status room-agent-llm.service --no-pager
```

The player verifies each pause/continue result through Chromium's native MPRIS
media interface. No screen coordinates or generic keyboard automation are used.

The dedicated kiosk profile uses Chromium's basic local password store because
desktop auto-login cannot unlock the graphical keyring. This prevents an
“Unlock Keyring” dialog from blocking unattended startup. Do not save personal
or other sensitive passwords in this demo-only browser profile.

## 2. Generate the pairing key on Windows

From this folder:

```powershell
python windows_companion.py --generate-key
```

The key is stored in `pairing.key`, which is excluded from source control. Do not paste or send its contents.

## 3. Copy the Pi-side files and key

From Windows PowerShell, while in this folder:

Replace `PI_IP` below with the Raspberry Pi's current LAN address:

```powershell
scp protocol.py intent_router.py local_llm.py openrouter_llm.py hermes_llm.py iphone_gateway.py configure_iphone.py configure_hermes.py configure_display.py display_manager.py lg_webos.py samsung_tizen.py display_endpoints.example.json agent_state.py respeaker_led.py respeaker_tuning.py led_test.py doa_test.py youtube_resolver.py podcasts.json pi_player.py room_canvas.py requirements-pi.txt 60-respeaker.rules pi_hub.py room-agent.service room-agent-llm.service room-agent-hermes.service hermes-config.example.yaml hermes.env.example IPHONE_PRODUCTION.md pairing.key voiceagent@PI_IP:/home/voiceagent/room-agent/
scp -r youtube_kiosk_extension room_canvas voiceagent@PI_IP:/home/voiceagent/room-agent/
```

If the destination does not exist yet, create it first over SSH:

```bash
mkdir -p /home/voiceagent/room-agent
```

After copying, protect the key on the Pi:

```bash
chmod 600 /home/voiceagent/room-agent/pairing.key
```

## 4. Prepare whisper.cpp on the Pi

Run over SSH:

```bash
sudo apt update
sudo apt install -y git cmake build-essential sox
cd /home/voiceagent
git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build
cmake --build build -j4 --config Release
sh models/download-ggml-model.sh base
```

If `whisper.cpp` already exists, skip the clone command and continue inside it.

Confirm the known recording on the Pi before networking:

```bash
./build/bin/whisper-cli -m models/ggml-base.bin -f /home/voiceagent/youtube-voice.wav -l en -nt --prompt "Open YouTube on the laptop. YouTube."
```

## 5. Enable ReSpeaker LED control

Install PyUSB from Debian and a narrowly scoped device-permission rule:

```bash
sudo apt install -y python3-usb
cd /home/voiceagent/room-agent
sudo install -m 0644 60-respeaker.rules /etc/udev/rules.d/60-respeaker.rules
sudo udevadm control --reload-rules
```

Confirm that `plugdev` appears in the current user's groups:

```bash
id -nG
```

If it is missing, add the current account and then log out and back in:

```bash
sudo usermod -aG plugdev voiceagent
```

Unplug and reconnect the ReSpeaker, then run the isolated LED test as the normal user:

```bash
python3 led_test.py
```

Do not run the complete voice hub with `sudo`. If LED access fails, the hub logs a warning and continues without visual feedback. Pass `--no-led` to deliberately disable it.

The implementation uses the documented USB interface for device `2886:0018` and the color/brightness commands exposed by the official [ReSpeaker Pixel Ring project](https://github.com/respeaker/pixel_ring). It talks to that small interface through PyUSB rather than copying or vendoring the complete Pixel Ring package.

### Validate voice direction

The normal `LISTENING` state now enables the ReSpeaker firmware's built-in voice-activity/direction pattern. The LEDs should follow the active speaker while the Pi records; no additional Python process is required.

For a deliberate 20-second physical check, run:

```bash
python3 doa_test.py --seconds 20
```

Speak from the front, right, back and left. The command temporarily opens the six-channel microphone stream (discarding its audio), prints the raw direction in degrees and draws a three-LED wedge toward it. It closes the stream and returns the ring to cyan `READY` when done. The absolute zero-degree orientation can differ by board/build configuration; if the displayed custom wedge needs rotating, pass a clockwise calibration such as `--offset 90`. This offset affects only the calibration display, not the firmware-managed listening pattern.

The read-only angle and voice-activity parameters follow Seeed's official [USB 4 Mic Array tuning interface](https://github.com/respeaker/usb_4_mic_array/blob/master/tuning.py). Individual LED addressing follows the official [Pixel Ring USB protocol](https://wiki.seeedstudio.com/ReSpeaker-USB-Mic-Array/).

## 6. Find the notebook IP

On Windows:

```powershell
ipconfig
```

Use the notebook IPv4 address for the same trusted local network as the Pi.

## 7. Start the notebook companion

From `room_agent` on Windows:

```powershell
python windows_companion.py
```

Windows Firewall may ask for permission. Allow only the appropriate private/domain network, subject to corporate policy. Leave this terminal running.

From the Pi, check reachability in another SSH window:

```bash
curl http://NOTEBOOK_IP:8766/health
```

Expected response includes `"status":"ok"`, both registered capabilities and `"youtube_api_configured":true`.

## 8. Test signed routing without audio

On the Pi:

```bash
cd /home/voiceagent/room-agent
python3 pi_hub.py --pc-url http://NOTEBOOK_IP:8766 --text "Open YouTube on the laptop"
```

The notebook should open YouTube. An unsigned request or an unapproved action is rejected.

Test the newest Cinkátko episode through the complete signed path:

```bash
python3 pi_hub.py \
  --pc-url http://NOTEBOOK_IP:8766 \
  --text "Play the latest Cinkatko podcast on the laptop"
```

The companion should print/open the selected episode rather than the YouTube homepage.

## 9. Run the live Pi voice flow

On the Pi:

```bash
cd /home/voiceagent/room-agent
python3 pi_hub.py --pc-url http://NOTEBOOK_IP:8766 --language en
```

Press Enter, wait for `LISTENING`, and say:

> Open YouTube on the laptop.

or:

> Play the latest Cinkátko podcast on the laptop.

For direct HDMI playback, omit `--pc-url` and say:

> Play the latest Cinkátko podcast on the TV.

The Pi records seven seconds, transcribes locally and routes the typed action. A
TV target starts the local HDMI player; a notebook target is signed and sent to
the companion.

## 10. Use a press-to-listen USB button

The hub accepts any standard USB HID button programmed to send the Enter key. No button-specific driver or additional resident Python process is needed. Until the dedicated button is attached, use an ordinary keyboard Enter key to validate the same flow:

```bash
cd /home/voiceagent/room-agent
python3 pi_hub.py \
  --pc-url http://NOTEBOOK_IP:8766 \
  --language en \
  --button-mode
```

Behavior:

1. Cyan `READY`: the microphone is not capturing; press the button.
2. Directional blue `LISTENING`: speak one command during the bounded window.
3. Amber/purple: local transcription and routing are in progress.
4. Green success or red error, followed automatically by cyan `READY`.
5. Press the button for the next command, or press `Ctrl+C` to stop the hub.

Button presses made while a command is already being processed are discarded before the next ready prompt. This prevents an accidental double press from silently opening another recording window. Connect the dedicated HID button to the Pi, not the corporate notebook, and configure it to emit only Enter.

## Security properties of this checkpoint

- HMAC-SHA256 request authentication.
- Thirty-second timestamp window.
- One-time nonces prevent request replay.
- Maximum request body of 8 KiB.
- Typed action and target fields.
- OpenRouter credentials are read only from the ignored environment file; the
  authenticated client uses a fixed HTTPS endpoint and never logs the key.
- Cloud output must match the strict room-action JSON Schema, then passes through
  the same local allowlist and argument validation as local-model output.
- The notebook accepts `web.open` only for the predefined `youtube` and `miton` sites.
- The pinned newest-episode capability accepts only `cinkatko` and `latest_episode`.
- Free-form podcast text is length-limited and used only as a catalog lookup or
  YouTube channel-search query; it never becomes a URL, shell command or browser
  instruction.
- Google queries are length-limited and URL-encoded into a fixed provider origin.
- Channel search candidates are resolved to their uploads playlists before a video
  is selected, and derivative clip/highlight channels are penalized.
- Resolved YouTube IDs must match the exact 11-character ID format.
- The kiosk helper runs only on YouTube and Google consent origins, accepts the
  consent screen, and attempts initial playback once. It does not resume a video
  after a later voice pause.
- No arbitrary URL, program, shell, keyboard, or mouse operation exists.

The hub emits validated lifecycle events for `READY`, `LISTENING`, `TRANSCRIBING`, `ROUTING`, `EXECUTING`, `SUCCESS` and `ERROR`. The LED and Room Canvas observers render those states while remaining isolated from command execution, and the listening LED pattern follows the detected voice direction. Button mode rearms only after the previous attempt finishes and discards queued presses. The official API flow follows the documented YouTube [channel search](https://developers.google.com/youtube/v3/docs/search/list), [channel uploads playlist](https://developers.google.com/youtube/v3/docs/channels), [playlist items](https://developers.google.com/youtube/v3/docs/playlistItems/list) and [video details](https://developers.google.com/youtube/v3/docs/videos). The next iteration adds a cached resolver fallback and measured meeting-room reliability.
