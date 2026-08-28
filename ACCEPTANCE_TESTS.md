# Voice Agent Acceptance Tests

This document turns the project plan into observable pass/fail tests. The boss demo is not considered ready merely because it worked once on the developer’s desk.

## 1. Test result format

Every recorded test result should include:

```text
Test ID:
Date/time:
Build/commit:
OS image/package lock:
STT engine/model:
Executor:
TV/display and HDMI path:
Network:
Speaker and distance:
Ambient condition:
Expected result:
Observed result:
Latency:
Result: PASS | FAIL | BLOCKED
Notes/error code:
```

Automated test runs should also emit the JSONL metric format described in `PROJECT_PLAN.md`.

## 2. Unit test gate

All unit tests must pass on Windows and Raspberry Pi ARM64.

### U-AUDIO — Audio/endpointing logic

- Pre-roll is included when speech begins.
- Speech shorter than the configured minimum is rejected.
- Silence ends an utterance within the configured window.
- A maximum-length utterance is forcibly bounded.
- A zero/silent stream produces a stable error rather than an empty transcription loop.
- Audio buffers are released after every attempt.

### U-NORM — Text normalization

- Czech accents are preserved in the primary normalized representation.
- Accent-folded matching maps `cinkátko` and `cinkatko` to the same alias.
- Case, duplicate whitespace, and normal punctuation do not affect parsing.
- Unicode lookalikes and unexpected punctuation do not reach a shell.
- Irrelevant words containing a small edit distance from `Miton` do not automatically match.

### U-INTENT — Intent grammar

Expected valid examples:

| Utterance | Expected selector | Expected channel |
|---|---|---|
| Pusť poslední Cinkátko. | latest_episode | cinkatko |
| Pusť nejnovější díl Cinkátka na YouTube. | latest_episode | cinkatko |
| Přehraj poslední video od Mitonu. | latest_episode (MVP product default) | cinkatko |
| Play the latest Cinkátko episode. | latest_episode | cinkatko |
| Na televizi pusť Cinkátko. | latest_episode | cinkatko |

Expected non-launch examples:

- “Cinkátko je dobrý název.”
- “Co je dnes na oběd?”
- “Nepouštěj Cinkátko.”
- “Pusť něco.”
- “Pusť poslední video.”
- An utterance naming two conflicting channels or targets.

Negation handling is mandatory. A phrase containing a clear Czech or English negation around the action must not launch content.

### U-YT — YouTube resolver

Use recorded/synthetic API fixtures for:

- Valid newest long-form public episode.
- Newest upload is a short clip; `latest_episode` selects the next valid long-form item.
- `latest_upload` selects the newest short clip.
- Upcoming live broadcast is newest.
- Completed live broadcast.
- Private/deleted playlist entry.
- Region-blocked candidate.
- Malformed duration.
- Empty channel.
- API timeout with valid cache.
- API timeout with expired cache and pinned fallback.
- API timeout with no fallback.
- Cache file is truncated or malformed.
- Candidates returned out of chronological order.

### U-EXEC — Executor safety

- Only an 11-character validated YouTube video ID is accepted.
- URLs are constructed internally, not accepted as arbitrary transcript text.
- ADB is invoked with an argument array.
- No user-controlled value is interpolated into a shell command.
- Executor timeout returns a stable error.
- Success requires a player/ADB confirmation appropriate to the endpoint.

### U-STATE — Orchestrator transitions

- Happy path reaches PLAYING then DISARMED.
- NoMatch returns to READY.
- STT timeout returns to READY.
- Resolver fallback still records the degraded source.
- Playback failure returns to READY after showing an error.
- A fatal startup error never displays READY.
- Two simultaneous utterances cannot create two playback jobs.

## 3. Hardware integration gate

### H-01 — ReSpeaker enumeration

Steps:

1. Boot with only the ReSpeaker attached.
2. Record the selected stable device identity.
3. Reboot three times.
4. Attach another USB audio device and reboot.

Pass:

- The ReSpeaker remains selected by stable name.
- No hardcoded transient card index causes the wrong device to be captured.

### H-02 — Firmware/channel mapping

Steps:

1. Enumerate supported channel counts at 16 kHz.
2. Record six-channel audio if available.
3. Speak from multiple directions.
4. Inspect/listen to each channel.

Pass:

- Firmware is classified as one-channel or six-channel.
- Processed ASR channel is explicitly identified.
- The application captures only the intended channel.

### H-03 — Capture quality

Conditions:

- 1 m quiet.
- 3 m quiet.
- Actual intended placement.
- HVAC/background noise.
- TV at intended demo volume before the listener disarms.

Pass:

- No clipping during normal speech.
- Speech is audibly intelligible.
- Endpointing does not clip the first or last word in more than 1 of 20 samples.

### H-04 — USB/power stability

Steps:

1. Run the Pi and ReSpeaker for one hour using the official Pi supply.
2. Exercise repeated STT and browser playback.
3. Inspect undervoltage indicators and USB disconnects.

Pass:

- No undervoltage warning.
- No USB audio disconnect.
- No spontaneous reboot.

### H-05 — Thermal soak

Steps:

1. Start at normal room temperature.
2. Execute representative commands repeatedly for 30 minutes.
3. Record temperature and STT latency for every attempt.

Pass:

- No fatal thermal condition.
- Median latency in the final ten attempts is no more than 25% worse than in the first ten.
- Any throttling is documented and does not break the overall latency gate.

### H-06 — Physical room layout

Pass:

- All cables reach without tension or trip hazards.
- ReSpeaker can sit in its intended acoustic position.
- Power adapters fit available outlets.
- No HDMI or USB cable must be swapped during the live demo.

## 4. Speech recognition gate

### Corpus

Minimum recommended room corpus:

- 6 accepted phrasings.
- 3 speakers where available.
- 2 distances.
- Quiet and normal-noise conditions.
- 3 repetitions per combination.
- At least 20 irrelevant/negative phrases.

If three speakers are unavailable, document the limitation rather than presenting a single-speaker result as general accuracy.

### Metrics

- Word error rate is useful diagnostically but is not the acceptance metric.
- Primary metric: correct typed intent.
- Secondary metrics: endpoint quality, correct channel entity, correct selector, STT latency.

### Pass criteria

- At least 95% correct typed intents across valid room utterances.
- 100% correct channel selection for accepted attempts.
- Zero launches from irrelevant/negative corpus.
- Median STT latency <= 2.0 s.
- P95 STT latency <= 4.0 s.

## 5. YouTube live integration gate

### Y-01 — Handle resolution

- `@mitoncz` resolves to the expected Miton Cinkátko channel.
- Resolved channel ID and uploads playlist are cached.

### Y-02 — Latest selection

- Operator compares the selected title/date to the visible channel page.
- `latest_episode` product semantics are signed off.
- If the newest upload is under the configured duration threshold, the chosen fallback is intentional.

### Y-03 — Cache behaviour

Steps:

1. Resolve live and populate cache.
2. Block/disable the API endpoint while leaving local files available.
3. Issue the same command.

Pass:

- Cached item plays within the API timeout budget.
- UI/logs clearly record `cache`, not `live_api`.

### Y-04 — Corrupt cache

Pass:

- Agent ignores malformed cache safely.
- It uses the approved pinned fallback or reports an actionable error.

### Y-05 — Credential leakage

Pass:

- API key is absent from Git status/diff, browser UI, ordinary logs, metrics, screenshots, and error messages.

## 6. Kiosk executor gate

### K-01 — Cold boot

Repeat ten times from Pi power-on.

Pass:

- Backend and browser start unattended.
- READY appears within the agreed boot budget, initially 60 seconds.
- A spoken command starts the correct video.

### K-02 — Autoplay

Test:

- New browser profile.
- Retained demo profile.
- Browser restart.
- Pi reboot.
- Video with a pre-roll advertisement if encountered.

Pass:

- The selected documented mitigation consistently produces audible playback.
- `PLAYING` is received before success is recorded.
- `onAutoplayBlocked` produces an explicit error/fallback, never false success.

### K-03 — Full screen and UI cleanliness

Pass:

- No browser chrome, restore prompt, first-run prompt, password prompt, desktop notification, or terminal is visible.
- Errors are readable from normal viewing distance.
- The cursor is not distractingly visible over playback.

### K-04 — Browser recovery

Steps:

1. Kill the Chromium process.
2. Kill the backend process.
3. Disconnect/reconnect HDMI.

Pass:

- Browser is restored by the documented supervisor/autostart mechanism.
- Backend restarts under systemd.
- Device returns to READY without reimaging.

### K-05 — Audio output

Pass:

- TV is the selected output after every reboot.
- Volume is audible and not muted.
- ReSpeaker’s 3.5 mm output is not accidentally selected.

### K-06 — Presentation account and ads

Pass:

- The retained profile uses the approved demo account, not a personal account.
- No login, consent, profile-picker, or Premium upsell interrupts the tested path.
- Personal recommendations, notifications, and account details are not exposed.
- Advertisement behaviour is acceptable to the presentation owner.
- The application does not suppress, skip, cover, or interfere with ads.

## 7. Smart-display capability gate

### D-01 — Enrolled-device isolation

Pass:

- Only endpoints present in the private enrollment file are probed for control.
- An unpaired TV discovered elsewhere on the LAN is never selected.
- When two enrolled endpoints are online, configured priority produces the same
  choice on every run.

### D-02 — Per-action capability selection

Pass:

- Native YouTube is preferred only when the installed app probe succeeds.
- Native Netflix is launched only when that exact app is discovered/enrolled.
- Google search uses a native endpoint only when a concrete browser activity is
  resolved; Apple TV and browserless Google TV use HDMI fallback.
- A reachable older Samsung with no verified executor is reported as smart but
  publishes no executable capability.
- When no native capability exists and HDMI is disconnected, the agent reports
  a failure rather than false success.

### D-03 — Session affinity

Pass:

- Pause, continue and close target the endpoint that started the active session.
- A media-control failure never jumps to another enrolled TV or to Pi Chromium.
- Close returns a native endpoint to its home screen without revealing Pi OS.

### D-04 — Native failure fallback

Twenty runs alternate a healthy native endpoint with a deliberately unreachable
one.

Pass:

- Stateless YouTube/browser actions fall back to HDMI only when HDMI exposes the
  same requested capability.
- The result records the failed endpoint and fallback reason.
- Native-only apps such as Netflix fail explicitly when unavailable; they are not
  converted into arbitrary browser URLs.

### D-05 — LG webOS pairing and native actions

With the Pi and one powered-on LG webOS TV on the same LAN, complete the pairing
prompt once, then reboot both devices before repeating the checks.

Pass:

- Automatic discovery returns the intended TV, or explicit-IP enrollment works
  when multicast is unavailable.
- The TV asks for approval only during initial enrollment; the saved key works
  after both devices reboot.
- The live capability report reflects the TV's installed Netflix, YouTube and
  browser apps rather than fixed app identifiers.
- Netflix and YouTube open in the native LG interface without exposing Pi OS.
- An exact YouTube video ID is handed to the native YouTube app; visible playback
  begins on at least nine of ten attempts.
- Pause, continue and close affect that LG session, and close returns to LG Home.
- Google search uses the LG browser only when `web.search` was published; a TV
  without that capability uses the HDMI fallback.

## 8. Google TV ADB gate

This section is required only before declaring the native-TV executor ready.

### A-01 — Pair and reconnect

Pass:

- Pi pairs successfully over the intended network.
- Connection returns after Pi reboot.
- Connection returns after Streamer reboot.
- Dynamic addressing/discovery behaviour is understood and documented.

### A-02 — Deep-link matrix

Ten repetitions for each starting condition:

- Google TV home screen.
- YouTube closed/force-stopped.
- YouTube open on another video.
- Streamer awake but idle.
- Streamer asleep, if wake is in scope.
- Netflix installed and closed.
- Browser installed and configured as the sole/default handler.
- No browser installed.

Pass:

- Correct video starts without directional menu automation.
- Failure is detected and reported within five seconds.
- App and browser capability probes match what is actually installed.
- Browserless Google search selects the tested HDMI fallback.
- Pause and resume are confirmed through Android's media-session state.

### A-03 — CEC isolation

Test separately:

- Direct Streamer-to-TV.
- Actual conference-room HDMI chain.
- TV already on/wrong input.
- TV in standby.

Pass for optional CEC feature:

- At least 19/20 successful wake/input operations in the actual room.

Failure of this test does not block the native playback executor if the runbook pre-powers and preselects the TV.

### A-04 — Security cleanup

Pass:

- Operator can unpair/disable wireless debugging using documented steps.
- No debug access remains enabled unintentionally after the experiment.

## 9. End-to-end boss-demo gate

Run in the actual room with final packed hardware and frozen build.

### E2E-01 — Twenty-run reliability

Use a shuffled mix of accepted Czech/English phrasings and speakers.

Pass:

- At least 19 of 20 attempts result in the correct video playing.
- No incorrect video launches.
- A failed attempt visibly rearms and succeeds on retry.

### E2E-02 — Latency

Measure from end of utterance to confirmed player `PLAYING`.

Pass:

- Median <= 4.0 s.
- P95 <= 7.0 s.
- No attempt exceeds 10 s without surfacing an error.

### E2E-03 — False activation soak

Play normal TV/podcast content for 30 minutes with the listener in its intended armed/disarmed state.

Pass:

- Zero unintended launches.

### E2E-04 — API degradation

Pass:

- With API unavailable and cache valid, the command still starts the cached approved video.

### E2E-05 — Operator recovery

Ask a second person to follow the runbook for:

- Normal startup.
- Re-arm after NoMatch.
- Switch to laptop fallback.
- Identify fatal preflight failure.

Pass:

- They can perform each action without undocumented terminal knowledge.

## 10. Stop-ship conditions

Do not use the system for the boss demo if any of these remain true:

- Actual-room reliability is below 19/20.
- Autoplay success is inferred rather than confirmed.
- ReSpeaker device selection changes across reboot.
- The selected latest video is not product-defined and manually verified.
- A credential appears in source control or presentation-visible logs.
- TV input/power depends on untested CEC.
- Corporate network use lacks approval where required.
- Google TV wireless debugging is enabled on an uncontrolled shared network.
- Raw meeting-room audio is being retained without a clear purpose and consent.
- The laptop fallback has not been rehearsed.
