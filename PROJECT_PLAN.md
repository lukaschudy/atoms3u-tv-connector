# Voice-Controlled YouTube/TV Demo — Project Plan

Status: planning baseline

Date: 2026-08-04
Primary demonstration: “Play the latest Cinkátko episode on YouTube.”

## 1. Outcome and scope

The first deliverable is a reliable, portable demonstration in which a person speaks a command in a meeting room and the latest appropriate Cinkátko video starts playing on the room television.

The plan deliberately separates the voice agent from the playback endpoint. The same voice, intent, and YouTube-resolution pipeline must support:

1. A local full-screen player on the development laptop.
2. A local full-screen player on the Raspberry Pi connected directly to the TV.
3. A native YouTube app on the Google TV Streamer, controlled through ADB.

The first boss-facing MVP ends at item 2. Item 3 is an independently gated follow-up experiment. Cast and generic graphical UI automation are not MVP dependencies.

Presentation language must be precise: the Pi-direct MVP demonstrates voice-controlled media playback *on a television display*. It does not demonstrate control of the television’s native operating system. The separately gated Google TV executor is the experiment that demonstrates control of a native TV endpoint.

### In scope for the MVP

- One-shot, far-field voice capture using the ReSpeaker.
- Czech and English variants of one well-defined command family.
- Deterministic intent parsing without an LLM.
- Official YouTube Data API lookup for the Cinkátko channel.
- Selection of the newest suitable public episode.
- Full-screen playback on a laptop and Raspberry Pi.
- Automatic startup on Raspberry Pi.
- Visible state, health checks, structured timing metrics, caching, and graceful failure.
- A repeatable boss-demo runbook and a tested fallback path.

### Explicitly outside the first MVP

- Arbitrary desktop control.
- General conversational interaction.
- A custom Czech wake word.
- Voice responses or text-to-speech.
- Reliable voice interruption while loud TV audio is playing.
- Automatic TV power/input switching as a required demo step.
- Production-grade Cast integration.
- Supporting arbitrary YouTube channels discovered from free-form speech.

## 2. Architecture decisions

### D1 — Primary endpoint: Raspberry Pi directly connected to the TV

The Pi runs the voice agent and the full-screen YouTube player. This removes all peer-to-peer networking, ADB, mDNS, client-isolation, and Google TV debugging dependencies from the boss demo.

The TV will be turned on and switched to the Pi input before the demonstration. HDMI-CEC is a later enhancement, not a success dependency.

### D2 — Development and emergency fallback: Windows laptop

The Windows laptop runs the same backend and browser UI. It is used to develop and validate the voice pipeline before deploying to ARM64. On demo day it remains the recovery endpoint if the Pi, SD card, or Pi browser fails.

### D3 — Native-TV experiment: Google TV Streamer through ADB

ADB is implemented behind the same playback interface after the Pi-direct version passes its reliability gate. The Pi becomes headless in this configuration; it does not need its HDMI connection. This version demonstrates control of a separate native TV endpoint but requires a suitable local network and enabled developer access.

### D4 — No LLM in the initial command path

For one channel and a small command grammar, deterministic parsing is faster, cheaper, more observable, and less likely to surprise during the demonstration. An LLM may later map broader natural-language requests into the same typed intent object.

### D5 — Local speech-to-text first, cloud fallback only if needed

The first candidate is multilingual whisper.cpp using `tiny` and `base` models, benchmarked on the actual Pi and ReSpeaker recordings. A quantized model is primarily a memory/storage optimization; it must not be assumed faster without measurement.

If local recognition fails the room-accuracy or latency gate, the architecture permits replacing only the STT adapter with a cloud service. Continuous room audio must never be uploaded; only the bounded post-trigger utterance may be sent.

### D6 — One-shot listener instead of a wake word

For the first demo, the service arms after startup, shows a ready state, waits for a valid command, executes it, and disarms during playback. This avoids false triggers from the TV and avoids building or training a wake-word model.

If the captured utterance is not a valid command, the agent remains armed. A wake word is a later addition.

### D7 — Official YouTube APIs and player

Public metadata is resolved through YouTube Data API v3. Playback uses the regular YouTube player or IFrame Player API. The project will not extract streams or depend on `yt-dlp` for the normal path.

The final browser/Google TV profile must use a company-approved demo Google account. A YouTube Premium account is strongly preferred for presentation predictability; the system must not suppress, skip, cover, or otherwise interfere with advertisements. Personal recommendations, account names, notifications, and watch history must not appear unexpectedly on the presentation screen.

### D8 — Reliability before TV automation

The first boss demo begins with the TV powered on, volume set, and input selected. CEC power-on and input switching are only enabled after being tested against the exact room display and any intervening AVR, wall plate, matrix, or extender.

## 3. System state flow

The orchestrator owns a small explicit state machine:

```text
BOOTING
  -> SELF_TEST
  -> READY
  -> RECORDING
  -> TRANSCRIBING
  -> PARSING
  -> RESOLVING
  -> EXECUTING
  -> PLAYING
  -> DISARMED

Any active state
  -> RECOVERABLE_ERROR -> READY
  -> FATAL_ERROR
```

Expected user-visible meanings:

| State | UI/LED intention | Behaviour |
|---|---|---|
| BOOTING | Purple | Services are starting. |
| SELF_TEST | Purple pulse | Microphone, model, cache, network, and executor are checked. |
| READY | Blue | Waiting for a spoken command. |
| RECORDING | Red | Speech is currently being captured. |
| TRANSCRIBING/PARSING/RESOLVING | Yellow | The bounded utterance is being processed. |
| EXECUTING | Yellow pulse | Playback command has been sent. |
| PLAYING/DISARMED | Green, then off | Video has started and listening is disabled. |
| RECOVERABLE_ERROR | Red blink, then blue | Error is logged and the listener rearms. |
| FATAL_ERROR | Solid red | Preflight or operator attention is required. |

ReSpeaker LED control is optional for the first technical spike because its HID examples are old. The full-screen UI must display the same status and remains the required indicator. Audio capture must not depend on LED support.

## 4. Typed interfaces

The implementation should depend on small contracts rather than particular libraries.

### Audio capture

```text
AudioSource.healthcheck() -> Health
AudioSource.frames() -> stream[PCMFrame]
EndpointDetector.accept(frame) -> START | CONTINUE | END | NONE
```

### Speech-to-text

```text
SpeechToText.transcribe(pcm, language_hint, prompt) -> Transcript

Transcript:
  text
  detected_language
  confidence_or_quality_signal
  duration_ms
  engine
  model
```

Whisper does not provide a universally calibrated confidence score. The adapter may expose average token probability, no-speech probability, fallbacks, or `unknown`. Business logic must not pretend scores from different engines are comparable.

### Intent parser

```text
IntentParser.parse(transcript) -> PlayIntent | NoMatch | Ambiguous

PlayIntent:
  action: play
  service: youtube
  selector: latest_upload | latest_episode
  channel_key: cinkatko
  target: default | local_display | google_tv
```

### Content resolver

```text
ContentResolver.resolve(intent) -> MediaItem

MediaItem:
  provider: youtube
  video_id
  title
  channel_id
  channel_title
  published_at
  duration
  watch_url
  resolved_at
  source: live_api | cache | pinned_fallback
```

### Playback executor

```text
PlaybackExecutor.healthcheck() -> Health
PlaybackExecutor.play(media_item) -> PlaybackResult
PlaybackExecutor.stop() -> Result
PlaybackExecutor.pause() -> Result
PlaybackExecutor.resume() -> Result
```

Only `healthcheck` and `play` are required for the first MVP. Other methods establish the extension boundary.

## 5. Proposed repository structure

Technology baseline:

- Python 3 for orchestration, audio buffering, API calls, state, and metrics.
- A native pinned whisper.cpp build/model behind the STT adapter.
- A small loopback-only Python web service for UI events.
- Vanilla HTML, CSS, and JavaScript for the kiosk UI; no Node build pipeline is required.
- Direct HTTPS calls to YouTube Data API instead of a large Google client dependency.
- `pytest` for deterministic unit/integration tests.
- A normal Python virtual environment on Windows/Pi; Docker is intentionally avoided for the MVP because host audio, HDMI, Wayland, and browser integration are the difficult parts.

The exact audio and web libraries should be selected in the Windows vertical slice and then pinned only after ARM64 installation is proven. The architecture must not expose those choices outside their adapters.

```text
voice-device/
  pyproject.toml
  README.md
  config/
    agent.example.yaml
    channels.yaml
  src/voice_agent/
    app.py
    state.py
    config.py
    audio/
      source.py
      endpointing.py
    stt/
      base.py
      whisper_cpp.py
    intent/
      models.py
      normalize.py
      parser.py
    content/
      youtube.py
      cache.py
  executors/
      base.py
      kiosk.py
      adb_google_tv.py
      windows_companion.py  # future, only if Pi must control a separate PC
    ui/
      server.py
    health.py
    metrics.py
  ui/
    index.html
    app.js
    styles.css
  tests/
    fixtures/
    test_normalize.py
    test_parser.py
    test_youtube_resolver.py
    test_state_machine.py
    test_executor_commands.py
  deploy/
    voice-agent.service
    labwc-autostart.example
  scripts/
    preflight.py
    benchmark_stt.py
    record_test_corpus.py
  state/
    .gitkeep
  PROJECT_PLAN.md
  ACCEPTANCE_TESTS.md
  DEMO_RUNBOOK.md
```

Secrets, cached API responses, recordings, and runtime logs must be excluded from version control.

## 6. Audio pipeline

### Device discovery

At startup:

1. Enumerate USB audio capture devices.
2. Select a device whose stable identity contains `ReSpeaker`, not a transient numeric card index.
3. Confirm 16 kHz, signed 16-bit PCM capture.
4. Detect whether firmware exposes one or six channels.
5. When six channels are exposed, select processed ASR channel 0 rather than downmixing every raw microphone channel.
6. Record a one-second sample and verify that it is not silent, clipped, or all zeros.

The numeric ALSA/PipeWire device index may change across reboots or when another USB audio device is attached; configuration must use a stable name match where possible.

### Endpointing for the first spike

Begin with a low-dependency adaptive energy endpoint detector:

- Maintain approximately 500 ms of pre-roll so the first syllable is not clipped.
- Estimate ambient energy while in READY.
- Start after speech-like energy exceeds the adaptive threshold for roughly 150–250 ms.
- End after roughly 700–1,000 ms of silence.
- Reject captures shorter than approximately 400 ms.
- Enforce a maximum utterance length of 12 seconds.
- Keep PCM in memory; do not write routine commands to disk.

If the room test shows poor endpointing, replace this component with an ONNX VAD or the ReSpeaker hardware VAD without changing downstream modules.

### Acoustic placement

- Put the microphone on the meeting table rather than immediately beside the TV speakers.
- Keep it away from laptop fans, HVAC vents, and hard reflective corners.
- Use a non-vibrating surface.
- Test the actual microphone-to-speaker distance at the intended TV volume.
- For the one-shot demo, disarm listening once playback begins.

### AEC limitation

The ReSpeaker can use a host playback reference for AEC. Audio produced by a separate Google TV Streamer is not automatically present in that reference channel. This is why always-on control during playback is a later work item.

## 7. Speech-to-text plan

### Candidate models

Benchmark multilingual, not `.en`, variants:

1. `tiny` — fastest candidate, lowest expected proper-name accuracy.
2. `base` — preferred starting quality candidate.
3. Quantized versions — compare memory, load time, thermals, and actual end-to-end latency; do not assume speedup.

The prompt/language configuration should bias recognition toward:

```text
Language: Czech
Expected names: Cinkátko, Miton, YouTube
Expected command: Pusť poslední díl Cinkátka na YouTube.
```

### Benchmark corpus

Create a consented, local corpus recorded through the actual ReSpeaker:

- At least three speakers if available.
- Czech and English.
- Distances of approximately 1 m and 3 m; add the real room distance.
- Quiet room and normal background noise.
- Exact command plus natural paraphrases.
- Include proper-name variants and deliberately irrelevant speech.

Example phrases:

- “Pusť poslední Cinkátko.”
- “Pusť nejnovější díl Cinkátka na YouTube.”
- “Přehraj poslední video od Mitonu.”
- “Na televizi pusť nejnovější epizodu Cinkátka.”
- “Play the latest Cinkátko episode on YouTube.”
- “Play the newest video from Miton.”

### Model acceptance gate

Select the smallest model that meets all of these on the recorded corpus:

- Correctly maps at least 95% of valid utterances to the expected typed intent after normalization.
- Produces no content launch for all irrelevant utterances.
- Median STT time no more than 2.0 seconds for a typical 4–7 second command.
- 95th-percentile STT time no more than 4.0 seconds.
- No thermal throttling that materially worsens repeated-command latency over a 30-minute soak.

If no local candidate passes, run a cloud-STT comparison on the same bounded recordings and document the privacy, credential, cost, network, and latency trade-off before switching.

## 8. Intent normalization and parsing

### Normalization

The parser should:

- Apply Unicode normalization.
- Lowercase text.
- Collapse whitespace and punctuation.
- Maintain both accented and accent-folded representations.
- Recognize aliases and known likely STT variants.
- Avoid unconstrained fuzzy matching that could launch unrelated content.

Initial channel aliases:

```yaml
cinkatko:
  handle: "@mitoncz"
  aliases:
    - "cinkátko"
    - "cinkatko"
    - "miton cinkátko"
    - "miton cinkatko"
    - "miton"
```

Likely misrecognitions should be added only after they are observed in the test corpus. They should not be guessed into an ever-growing unsafe list.

### Initial grammar

A valid intent needs:

- A play verb or equivalent request.
- A recognized channel alias.
- An optional latest/newest selector; default for Cinkátko is `latest_episode`.
- An optional target; default is the configured executor.

If a phrase names multiple channels or produces conflicting targets, return `Ambiguous` and do nothing.

### Safety rule

The parser emits typed intents only. Neither transcript text nor channel text may be inserted into a shell command. Executors receive validated video IDs matching the expected YouTube ID format.

## 9. YouTube content resolver

### Channel configuration

The known official handle is `@mitoncz`, whose current channel display name is Miton Cinkátko. The handle is configuration, not inferred by web search on every command.

### API sequence

1. `channels.list(part=contentDetails,snippet, forHandle=@mitoncz)`
2. Read `contentDetails.relatedPlaylists.uploads` and cache both channel ID and uploads-playlist ID.
3. `playlistItems.list(part=snippet,contentDetails,status, playlistId=..., maxResults=10)`
4. Sort candidates by `contentDetails.videoPublishedAt` rather than trusting response order alone.
5. `videos.list(part=snippet,contentDetails,status, id=...)` for candidate validation.

### Selection semantics

The words “latest video” and “latest episode” are not identical:

- `latest_upload`: newest playable public upload, including a short clip.
- `latest_episode`: newest playable public upload that satisfies the per-channel episode policy.

Initial Cinkátko episode policy:

- Public and playable.
- Not an upcoming broadcast.
- Not region-blocked in the deployment region.
- Embeddable when the selected kiosk player uses the IFrame API.
- Duration at least ten minutes, configurable.
- Prefer the newest publication timestamp.

YouTube does not expose one universal `isShort` field through this path, so duration is an explicit product heuristic rather than a claim about YouTube’s classification.

### Cache and fallback order

1. Try the live API with a short timeout.
2. If live resolution succeeds, atomically update the cache.
3. If it times out or returns a transient error, use a non-expired cached result.
4. If no valid cache exists, use an operator-approved pinned fallback video ID.
5. Clearly mark `source` in metrics so a cached result cannot masquerade as a live lookup.

Recommended cache fields:

```json
{
  "channel_key": "cinkatko",
  "selector": "latest_episode",
  "video_id": "...",
  "title": "...",
  "published_at": "...",
  "resolved_at": "...",
  "expires_at": "..."
}
```

Refresh the cache during demo preflight. Do not refresh dependencies or operating systems during demo preflight.

### Credentials

- Restrict the API key to YouTube Data API v3.
- Store it in a root/user-readable environment file with restrictive permissions.
- Never place it in YAML committed to Git.
- Rotate it if exposed in logs or a screen recording.

## 10. Pi/laptop kiosk executor

### UI process

A small local web application owns the visible experience:

- Status screen and error messages.
- Current transcript, optionally shown only during development.
- Resolved title and thumbnail.
- Full-screen YouTube player.
- A local WebSocket or server-sent event connection to the backend.
- Player state events, including `PLAYING`, error, and autoplay-blocked.

The service binds to loopback only for the direct-display MVP.

### Playback flow

1. Browser starts once at boot on the local status page.
2. Backend reaches READY and updates the UI.
3. Resolver emits a validated `MediaItem`.
4. Backend sends `play(video_id)` to the UI.
5. UI loads the video through the official YouTube player.
6. UI reports `PLAYING` back to the backend.
7. Backend records final latency and enters DISARMED.

### Autoplay gate

Unmuted scripted playback may be blocked by Chromium. The spike must explicitly test:

- Kiosk Chromium with the intended autoplay policy configuration.
- Cold boot with a new profile.
- Warm boot with the retained demo profile.
- Video with and without a pre-roll advertisement.
- Recovery after the browser process is killed and restarted.
- The exact signed-in demo account/profile, including whether Premium benefits apply to the selected player configuration.

The UI must surface `onAutoplayBlocked`; it must never report success merely because it sent `playVideo()`.

If the current Chromium build cannot provide reliable unmuted autoplay, choose one tested mitigation before proceeding:

1. A one-time operator activation during preflight that primes the retained browser profile.
2. A controlled local input event after the player visibly reaches the ready state.
3. Switching the boss-demo endpoint to the native Google TV executor if it proves more reliable in the actual room/network.

The mitigation must be documented and included in the runbook.

Ads are not an error and must never be programmatically bypassed. If an unpredictable pre-roll is unacceptable to the presentation, use an approved Premium demo account or change the presentation expectation. Confirm during preflight that the profile contains no personal account information or distracting recommendations.

### Raspberry Pi OS deployment

- Use the current stable Raspberry Pi OS 64-bit Desktop image.
- Record the exact image and package versions after the system passes acceptance.
- Enable Desktop Autologin for the kiosk user.
- Launch Chromium from the supported labwc autostart configuration.
- Run the backend under a supervised systemd service with restart-on-failure.
- Keep the graphical browser and backend responsibilities separate.

Official Raspberry Pi guidance currently uses `~/.config/labwc/autostart` for Chromium kiosk startup.

## 11. Google TV ADB executor

This executor is not allowed onto the boss-demo critical path until it passes its own gate.

### Setup

1. Connect the Streamer and Pi to the same approved local network.
2. Complete Google TV setup and YouTube installation/login.
3. Enable developer options and wireless debugging.
4. Pair the Pi using the displayed address, pairing port, and code.
5. Confirm automatic reconnection after reboot on the intended network.
6. Discover and record the installed YouTube TV package name.

### Launch concept

The executor constructs an Android `ACTION_VIEW` intent for a validated watch URL, restricted to the confirmed YouTube TV package. It runs commands as an argument array, never through an interpolated shell string.

Conceptual operation:

```text
adb shell am start -W \
  -a android.intent.action.VIEW \
  -d https://www.youtube.com/watch?v=VIDEO_ID \
  com.google.android.youtube.tv
```

### ADB gate

The executor must pass all of the following:

- Ten consecutive launches from the Google TV home screen.
- Ten consecutive launches when YouTube is already open on a different video.
- Reconnect and launch after a Streamer reboot.
- Reconnect and launch after a Pi reboot.
- Correct video starts without brittle directional navigation.
- Failure is reported within five seconds when the Streamer is unavailable.
- No command depends on a dynamic IP without discovery, reservation, or configuration.
- The exact room network permits the connection.

Test TV power/CEC separately. If CEC is unreliable, the TV remains on and input-selected just as in the Pi-direct runbook.

### Security

- Wireless debugging is administrative access and must be disabled or the Pi unpaired after the experiment when not needed.
- Do not leave ADB exposed on a shared corporate LAN.
- Prefer an isolated, IT-approved demo network.

## 12. Cast position

Cast is a research comparison after the ADB experiment, not a fallback for it.

Reasons:

- The official sender SDK targets Android, iOS, and supported web applications.
- The Default Media Receiver accepts actual media resources, not a YouTube watch page as a generic video URL.
- Headless Python libraries that control YouTube’s Cast receiver rely on unofficial or reverse-engineered behaviour.
- Discovery depends on local network/multicast behaviour that is commonly restricted on corporate networks.

If a production-supported Cast path is eventually required, scope it as a sender application project with explicit UX and platform requirements rather than a small Raspberry Pi library swap.

## 12A. Windows PC control position

The laptop vertical slice already demonstrates voice control of a PC because the agent runs locally and controls its own browser. This is the simplest PC scenario and uses the same kiosk executor.

If the eventual product requirement is “the Pi hears the command but a separate Windows PC executes it,” add a narrow Windows companion executor later:

- Install a small user-visible service on the Windows PC.
- Expose only typed, allowlisted actions such as `play_youtube(video_id)`, `pause`, `resume`, and `stop`.
- Authenticate the Pi with a rotatable token or mutually authenticated channel.
- Bind to an approved private interface/network and apply Windows Firewall rules.
- Never expose arbitrary PowerShell, command execution, mouse coordinates, or a generic remote shell as the voice interface.
- Return explicit accepted/playing/error acknowledgements to the Pi.

This remote-PC mode has the same corporate-network dependency as ADB and is not needed for the first TV demo. It should be gated with reboot, disconnect, authentication-failure, and wrong-target tests before use.

## 13. HDMI-CEC position

CEC is an enhancement with a device-specific acceptance test.

### Pi-direct CEC spike

- Confirm `/dev/cec*` exists with the intended micro-HDMI port and cable.
- Use Linux `cec-ctl` tooling to enumerate the TV.
- Test wake, active-source/input switch, power status, and standby separately.
- Repeat after cold boots.
- Repeat through the exact conference-room HDMI path.

### Streamer CEC spike

Google TV has CEC enabled by default, but any AVR, soundbar, wall plate, HDBaseT extender, or HDMI matrix may block or delay commands. Test direct-to-TV first and then the real room chain.

No boss-demo script should wait indefinitely for CEC. A failure must fall back to a clear operator instruction.

## 14. Health checks and observability

### Startup self-test

Required checks:

- ReSpeaker detected by stable name.
- Audio stream opens at expected format.
- A short capture contains plausible audio.
- STT model exists, checksum matches, and can transcribe a fixture.
- Configuration parses and known channel aliases are unique.
- YouTube cache parses and any pinned video ID is validly formatted.
- Internet/DNS and YouTube API availability are classified, not conflated.
- YouTube account/profile state, consent prompts, and expected advertisement behaviour are verified.
- Selected executor reports healthy.
- Local browser UI is connected for kiosk mode.
- ADB connection and YouTube package resolve for Google TV mode.
- System clock is synchronized sufficiently for HTTPS.
- Disk free space and CPU temperature are within reasonable operational bounds.

### Event metrics

One JSON line per command attempt:

```json
{
  "attempt_id": "uuid",
  "started_at": "timestamp",
  "audio_ms": 0,
  "stt_ms": 0,
  "parse_ms": 0,
  "resolve_ms": 0,
  "execute_ms": 0,
  "time_to_playing_ms": 0,
  "transcript": "...",
  "intent": "...",
  "video_id": "...",
  "resolution_source": "live_api|cache|pinned_fallback",
  "executor": "kiosk|adb_google_tv",
  "result": "success|no_match|error",
  "error_code": null,
  "temperature_c": 0
}
```

Raw audio is excluded. During an explicitly consented benchmark session only, recordings may be stored under a git-ignored directory and deleted when the benchmark is complete.

### Stable error codes

Examples:

- `AUDIO_DEVICE_MISSING`
- `AUDIO_SILENT`
- `STT_TIMEOUT`
- `INTENT_NO_MATCH`
- `INTENT_AMBIGUOUS`
- `YOUTUBE_API_TIMEOUT`
- `YOUTUBE_NO_CANDIDATE`
- `CACHE_MISSING`
- `AUTOPLAY_BLOCKED`
- `PLAYER_ERROR`
- `ADB_DISCONNECTED`
- `ADB_DEEP_LINK_FAILED`
- `PLAYBACK_NOT_CONFIRMED`

## 15. Security and privacy baseline

- Show a clear ready/recording indicator whenever the microphone is armed.
- Do not store continuous audio.
- Do not retain raw commands by default.
- Make transcript logging configurable and off for normal corporate demonstrations if required.
- Bind local APIs to loopback unless remote access is explicitly needed.
- Never implement a remote arbitrary-shell endpoint.
- Store secrets outside the repository with restrictive file permissions.
- Restrict outbound APIs and keys to the minimum services.
- Use a company-approved demo Google/YouTube account rather than a personal profile.
- Obtain internal approval before using a phone hotspot or travel router.
- Disable Google TV wireless debugging when the experiment is not active.
- If cloud STT is adopted, document provider, data retention, region, access controls, and attendee notice before use.

## 16. Physical configurations

### Configuration A — Laptop development/fallback

```text
ReSpeaker -> USB -> Windows laptop -> HDMI -> TV
```

Use the Streamer’s full-size HDMI cable. This configuration minimizes software-platform surprises and validates the full logical pipeline first.

### Configuration B — Primary dedicated MVP

```text
ReSpeaker -> USB -> Raspberry Pi -> micro-HDMI/HDMI -> TV
Raspberry Pi -> official 27 W supply
```

The likely missing item is room-length cabling. Choose either:

- Pi on the table with a longer HDMI cable; or
- Pi near the TV with a quality USB data extension to the table microphone.

### Configuration C — Native Google TV experiment

```text
ReSpeaker -> USB -> Raspberry Pi
Google TV Streamer -> HDMI -> TV
Pi and Streamer -> same approved local network
```

The Pi HDMI cable is removed. The Pi can remain on the meeting table with the microphone, which may improve acoustics and reduce long physical cabling.

### Streamer power check

The replacement supply must provide the required 5 V/1.5 A profile through a known-good cable. A charger’s total “20 W” marketing number alone does not establish cable/profile compatibility. Soak-test for low-power warnings and reboot loops.

## 17. Implementation phases and gates

Effort ranges are engineering estimates, not deadlines. Room/network problems can dominate the Google TV phase.

### Phase 0 — Room and hardware reconnaissance (1–2 hours)

Tasks:

- Record TV/display manufacturer and model.
- Photograph or diagram the HDMI path.
- Determine whether the wall input is direct, AVR-based, or matrix-based.
- Measure table-to-display cable distance.
- Identify power outlets.
- Determine corporate network type and whether peer devices may communicate.
- Bench-test Streamer power and cable.

Exit gate:

- A physical placement and network choice exist for each candidate configuration.
- No required cable or credential is unknown.

### Phase 1 — Windows vertical slice (4–6 hours)

Tasks:

- Capture processed ReSpeaker audio.
- Implement endpointing.
- Run two local STT candidates.
- Implement normalization and typed intent parser.
- Resolve the newest Cinkátko episode through YouTube API.
- Play through the local UI and confirm `PLAYING`.
- Write the first structured metric record.

Exit gate:

- Five consecutive end-to-end launches on the laptop.
- Invalid speech produces no launch.
- API failure uses the cache.

### Phase 2 — Raspberry Pi port (4–8 hours)

Tasks:

- Install current stable Raspberry Pi OS 64-bit Desktop.
- Confirm ReSpeaker capture and device naming across reboot.
- Build/install STT for ARM64.
- Run the same unit/integration tests.
- Configure systemd backend supervision and labwc kiosk startup.
- Test autoplay and browser recovery.
- Record CPU temperature and latency during a 30-minute soak.

Exit gate:

- Pi boots unattended to READY.
- Ten consecutive end-to-end launches.
- Browser crash recovers without reimaging.
- No unacceptable thermal latency drift.

### Phase 3 — MVP hardening (3–5 hours)

Tasks:

- Build the exact-room speech corpus.
- Tune endpointing and parser aliases from observed errors.
- Add preflight and operator-readable errors.
- Freeze package/model versions.
- Test live, cached, and pinned resolution.
- Complete runbook and packing checklist.
- Rehearse fallback to the laptop.

Exit gate:

- At least 19 successes in 20 actual-room dry runs.
- Median end-of-utterance to playback below four seconds.
- No false playback during a 30-minute room-noise/TV test.

### Phase 4 — Google TV ADB executor (3–6 hours plus network coordination)

Tasks:

- Establish approved local connectivity.
- Pair ADB and validate reconnection.
- Discover package and deep-link behaviour.
- Implement and test executor.
- Test awake/asleep/app-open/reboot scenarios.
- Test CEC separately.

Exit gate:

- All ADB gate tests in section 11 pass.
- Security/disable procedure is documented.
- Pi-direct remains available as fallback.

### Phase 5 — Comparative experiment (2–4 hours)

Compare Pi kiosk and Google TV ADB using identical utterances:

- Recognition success.
- Content-selection correctness.
- Dispatch success.
- Playback confirmation.
- Median and 95th-percentile latency.
- Recovery after endpoint/network failure.
- Operator setup time.

Cast can be scoped after these results if it answers a real product question.

## 18. Definition of done for the boss MVP

The MVP is complete when:

- The packed Pi setup boots unattended in the target room.
- A visible READY indicator appears.
- The intended command and agreed paraphrases select the correct latest Cinkátko episode.
- Playback begins full-screen with audio.
- The system confirms playback rather than assuming it.
- Invalid/background speech does not launch content.
- A YouTube metadata outage uses a known cached result.
- Errors are visible and recover without terminal access where practical.
- The actual-room dry-run pass rate is at least 19/20.
- The laptop fallback has been rehearsed.
- Raw room audio and credentials are not exposed.
- The runbook can be followed by someone other than the developer.

## 19. Principal risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---:|---:|---|
| Corporate network isolates Pi and Streamer | High | High for ADB/Cast | Keep Pi-direct as MVP; use approved test VLAN/hotspot/router for native-TV experiment. |
| Browser blocks unmuted autoplay | Medium | High | Detect explicit player event; prime profile or use tested input mitigation; gate before boss demo. |
| Cinkátko proper name is misrecognized | Medium | High | Czech model, initial prompt, aliases based on recordings, typed grammar. |
| Latest upload is a Short/teaser | Medium | Medium | Define `latest_episode` policy with duration threshold. |
| ReSpeaker channel selection is wrong | Medium | High | Discover 1/6-channel firmware and explicitly capture processed channel 0. |
| TV audio retriggers listener | High if always-on | High | One-shot listener disarms during playback. |
| Passive cooling increases STT latency | Low–Medium | Medium | Benchmark repeated commands and temperature; use smaller/cloud STT or cooling only if gate fails. |
| Streamer replacement power is unstable | Medium | High | Verify 5 V/1.5 A profile, cable, and one-hour soak. |
| HDMI CEC fails through room AV equipment | High | Medium | Pre-power and preselect input; CEC remains optional. |
| YouTube API unavailable/quota-limited | Low | Medium | Short timeout, cache, approved pinned fallback. |
| A pre-roll ad or personal recommendation appears | Medium | Medium–High | Use a company-approved Premium demo account/profile; verify the exact player during preflight; never bypass ads. |
| Internet unavailable entirely | Low–Medium | High | Preflight; laptop/hotspot contingency; optionally obtain a rights-cleared local company clip. |
| Google TV deep link changes behaviour | Medium | High for native endpoint | Ten-run/reboot gate; retain Pi kiosk executor. |
| SD corruption or Pi failure | Low | High | Image backup after freeze; laptop runs same stack. |
| Meeting-room privacy objection | Medium | High | Visible armed state, one-shot capture, no raw retention, local STT by default. |

## 20. Questions to settle before implementation freeze

These do not block the first laptop spike, but they affect the final room configuration:

1. What is the exact TV/display model?
2. Does HDMI connect directly to the TV or through a corporate AV system?
3. How far is the table microphone position from the HDMI input?
4. Is the native Google TV YouTube UI essential to the boss story, or is full-screen YouTube from the Pi acceptable?
5. Does the room network permit peer-to-peer devices, mDNS, and wireless debugging?
6. Are phone hotspots or travel routers permitted?
7. Is local-only speech processing a privacy requirement?
8. Confirm or override the MVP default: “latest” means the newest eligible full-length podcast episode, not the literal newest upload.
9. Should the demonstration accept both Czech and English commands?
10. Can Miton provide a rights-cleared local fallback clip for a total internet outage?
11. Is a company-approved YouTube Premium demo account available, and may its watch history be used for testing?

## 21. Research references

- Raspberry Pi kiosk mode and labwc autostart: https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/
- Raspberry Pi power and thermal management: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html
- Raspberry Pi 5 cooling tests: https://www.raspberrypi.com/news/heating-and-cooling-raspberry-pi-5/
- ReSpeaker USB Mic Array specification and firmware channels: https://wiki.seeedstudio.com/ReSpeaker-USB-Mic-Array/
- Android ADB and wireless debugging: https://developer.android.com/tools/adb
- Android App Links: https://developer.android.com/training/app-links/about
- Google TV Streamer specifications: https://support.google.com/chromecast/answer/3046409?hl=en
- Google TV Streamer box contents: https://support.google.com/chromecast/answer/15323053?hl=en
- Google TV low-power troubleshooting: https://support.google.com/chromecast/answer/10087405?hl=en
- Google TV network setup limits: https://support.google.com/googletv/answer/10050221?hl=en
- Google streaming-device AP isolation/captive portal guidance: https://support.google.com/chromecast/answer/7566322?hl=en-CA
- Google TV CEC troubleshooting: https://support.google.com/googletv/answer/16995384?hl=en
- YouTube channel handle lookup: https://developers.google.com/youtube/v3/docs/channels/list
- YouTube channel uploads playlist: https://developers.google.com/youtube/v3/docs/channels
- YouTube playlist items: https://developers.google.com/youtube/v3/docs/playlistItems
- YouTube video metadata/duration/live fields: https://developers.google.com/youtube/v3/docs/videos
- YouTube IFrame Player API: https://developers.google.com/youtube/iframe_api_reference
- YouTube for Android TV package listing: https://play.google.com/store/apps/details?id=com.google.android.youtube.tv
- Google Cast architecture: https://developers.google.com/cast/docs/overview
- Google Cast Web/Default Receiver: https://developers.google.com/cast/docs/web_receiver
- Cinkátko channel: https://www.youtube.com/@mitoncz
