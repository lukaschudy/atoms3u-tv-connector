# Boss Demo Runbook

This is the operational checklist for two deliberately separate display modes:

- **Native mode:** the headless Pi controls one enrolled and acceptance-tested
  Google/Android TV or Apple TV endpoint.
- **HDMI fallback mode:** the Pi renders its own Chromium/Room Canvas when the
  requested native capability is unavailable or the room has only a display.

Choose and rehearse the primary mode before the presentation. Do not enable a
new TV protocol in the room immediately before the demo.

## 1. Final packed configuration

```text
ReSpeaker -> USB data cable -> Raspberry Pi 5
Raspberry Pi 5 -> micro-HDMI/HDMI cable -> TV
Raspberry Pi 5 -> official 27 W USB-C power supply
Raspberry Pi 5 -> approved Wi-Fi or Ethernet with Internet access
```

In validated native mode, omit the Pi-to-TV HDMI cable from the active path and
keep it packed as the browser fallback. The Google TV Streamer or Apple TV must
already be connected to the television, paired, and on the same non-isolated
network as the Pi.

The Windows laptop, its charger, and a full-size HDMI cable are the tested fallback.

## 2. Packing checklist

Required:

- Raspberry Pi 5 in its passive case.
- Final labelled microSD card.
- A second imaged microSD card or verified image backup where practical.
- Official Raspberry Pi 27 W supply.
- ReSpeaker USB Mic Array.
- Tested USB-A to micro-USB data cable.
- Tested room-length micro-HDMI to HDMI cable, or tested USB extension according to the chosen layout.
- Access credentials for the approved network.
- TV remote or room-control access.
- Short printed/offline copy of this runbook.

Fallback:

- Windows laptop and charger.
- Full-size HDMI cable.
- Local copy of the same application, configuration, STT model, and approved cached video metadata.
- Google/YouTube login details only if the retained browser profile is not sufficient; do not expose a password during the presentation.

Useful contingency items:

- Small power strip/extension lead.
- Ethernet cable if the room provides an approved port.
- Spare known-good USB data cable.
- Spare known-good HDMI cable.
- Google TV or Apple TV remote when native mode is selected.

## 3. T-48 to T-24 hours

- Freeze the build, dependency versions, STT model, and Raspberry Pi OS image.
- Do not install optional upgrades after the final acceptance run.
- Verify the YouTube Data API key is active and restricted.
- Verify the retained browser profile uses the company-approved demo Google/YouTube account.
- Confirm whether the exact player/profile is ad-free through an approved Premium subscription; never install an ad blocker or automate ad skipping.
- Confirm no personal recommendations, profile names, notifications, consent prompts, or login prompts will appear.
- Refresh the Cinkátko channel/episode cache.
- Manually verify that the cached title is the intended latest full-length episode.
- Verify the pinned fallback video ID.
- Run all unit tests.
- Run the complete preflight.
- Reboot the Pi and perform 20 end-to-end attempts.
- Confirm the pass rate is at least 19/20.
- Confirm median and P95 latency meet the acceptance gate.
- Test laptop fallback from a cold start.
- Confirm no secrets or recordings are present in version control.
- Export/verify the recovery SD image.

If the target latest episode changes after this rehearsal, refresh and verify metadata only. Do not update the application stack.

## 4. T-60 minutes in the room

### Physical setup

1. Place the microphone in its tested table position.
2. Route cables safely without tension or trip hazards.
3. In HDMI mode, connect the Pi to the selected TV input. In native mode, leave
   the Pi headless and confirm the enrolled streamer/TV is already connected.
4. Connect the ReSpeaker before powering the Pi.
5. Connect network if using Ethernet.
6. Power the Pi using the official supply.

### TV setup

1. Turn the TV/display on manually.
2. In native mode, select the enrolled streamer/native interface. In HDMI mode,
   select the Pi’s HDMI input manually.
3. Set the intended volume.
4. Disable an aggressive screen saver or auto-input switch for the presentation window if permitted.
5. Do not enable or change CEC settings immediately before the demo.

### Wait for readiness

Pass condition:

- In HDMI mode, the local full-screen status UI appears. In native mode, the TV
  remains on its native interface and the ReSpeaker READY indication appears.
- SELF_TEST completes.
- READY is visible.
- No fatal error is displayed.

If READY does not appear within 60 seconds, follow the failure ladder below rather than repeatedly power-cycling without diagnosis.

## 5. T-45 minutes: technical preflight

Run the preflight action from the UI/operator control and verify:

- ReSpeaker detected by name.
- Live audio level moves when speaking.
- Capture format is 16 kHz and processed channel is selected.
- STT model checksum and fixture transcription pass.
- YouTube API live check succeeds, or a deliberate cache state is clearly shown.
- Selected Cinkátko title/date are visible and correct.
- Approved demo YouTube account/profile is active and its ad behaviour is known.
- Local player/UI connection is healthy.
- TV audio output is selected and not muted.
- CPU temperature is normal.
- Disk space and clock synchronization are healthy.
- `pi_hub.py --display-status --no-led` reports the intended endpoint online and
  lists the exact capability required by the demo action.
- No unintended conference-room endpoint appears in the enrollment file.

For an LG webOS rehearsal, complete its one-time on-screen pairing before this
preflight. Confirm that the capability report names `lg_webos`, lists the native
app needed by the demo, and lists `web.search` only when that exact TV exposes
its browser. Pairing is setup work and must not be deferred to the live demo.

Do one quiet-room command:

> “Pusť nejnovější díl Cinkátka na YouTube.”

Confirm audible playback, correct title, and the expected `DISPLAY:` endpoint.
Test pause, continue and close on that same endpoint.

## 6. T-30 minutes: fallback rehearsal

Without disturbing the Pi configuration:

1. Connect the laptop to an available/test input or verify the cable path.
2. Start the laptop version.
3. Confirm the ReSpeaker can be moved and selected if needed.
4. Confirm the same cached/resolved video plays.
5. Return the room to the Pi input and READY state.

This should take less than five minutes when rehearsed.

## 7. T-15 minutes: presentation state

- Close doors or reduce unnecessary background conversation.
- Leave the Pi on READY.
- Verify the intended current title one last time without changing dependencies.
- Clear development transcripts/log overlays from the visible UI.
- Keep the TV on and correct input selected.
- Keep the TV remote and laptop within reach but out of the main visual presentation.
- Ensure the microphone’s recording/ready indicator is understandable to attendees.
- Do not leave the agent listening to room conversation longer than necessary.

## 8. Suggested presentation sequence

### One-sentence setup

Explain that the small device listens locally for a bounded command, resolves the
newest episode through YouTube metadata, probes the enrolled screen’s capabilities,
and chooses either the native TV app or its own HDMI fallback. Describe native
control only when the visible `DISPLAY:` result names the native endpoint.

### Demonstration

Wait for READY, face roughly toward the table microphone, and say naturally:

> “Pusť na televizi nejnovější díl Cinkátka na YouTube.”

Expected visible progression:

```text
READY -> RECORDING -> PROCESSING -> resolved title -> PLAYING
```

Do not narrate over the utterance or processing interval.

### After playback begins

- Let the video play for a short, agreed interval.
- Point out that the listener is now disarmed so TV audio cannot trigger it.
- If native mode is in use, point out that the Pi remained headless and selected
  a verified app rather than displaying Raspberry Pi OS.

## 9. Failure ladder

Use the first applicable recovery. Do not improvise low-level changes in front of the audience.

### Level 1 — NoMatch or clipped speech

Symptoms:

- UI returns to READY.
- No incorrect video started.

Action:

1. Wait for READY.
2. Move slightly closer or speak toward the microphone.
3. Repeat the canonical command once.

### Level 2 — YouTube live-resolution failure

Symptoms:

- UI reports cache/pinned fallback.

Action:

- Continue if the shown cached title is the approved episode.
- Briefly state that the content resolver has a cached fallback.
- Do not enter an API key or debug quota live.

### Level 3 — Player/autoplay failure

Symptoms:

- Correct title resolved but playback is not confirmed.
- `AUTOPLAY_BLOCKED` or `PLAYER_ERROR` is shown.

Action:

1. Use the tested operator “Play/Retry” control once if that is the documented autoplay mitigation.
2. If it fails again, move directly to laptop fallback.

### Level 4 — Pi or browser failure

Symptoms:

- No READY state.
- Fatal audio/model error.
- Repeated browser crash.

Action:

1. Switch TV to the laptop HDMI input.
2. Move/connect the ReSpeaker to the laptop.
3. Start the tested laptop package/profile.
4. Verify READY and repeat the demo.

Do not reimage or update the Pi during the meeting.

### Level 5 — Complete Internet failure

Action options in order:

1. Use a pre-approved hotspot if permitted and rehearsed.
2. If Miton has supplied a rights-cleared local fallback clip, demonstrate the voice-to-local-playback control and clearly disclose that network content resolution is unavailable.
3. Otherwise stop and explain the failed external dependency; do not play an unauthorized downloaded copy.

### Level 6 — TV/room AV failure

Action:

- Use the room operator/control procedure.
- If the room HDMI path is unavailable, demonstrate on the laptop screen rather than changing CEC or AV-matrix settings blindly.

## 10. Error-code quick reference

| Error | Operator meaning | Action |
|---|---|---|
| AUDIO_DEVICE_MISSING | ReSpeaker is not detected | Reseat USB once; then laptop fallback. |
| AUDIO_SILENT | Device opened but audio is implausible | Check selected device/channel; laptop fallback if unresolved. |
| STT_TIMEOUT | Recognition exceeded limit | Re-arm and repeat once; then fallback. |
| INTENT_NO_MATCH | Safe rejection | Repeat canonical phrase. |
| YOUTUBE_API_TIMEOUT | Metadata service unavailable | Use verified cache if shown. |
| CACHE_MISSING | No safe resolved item | Do not improvise; use operator-approved fallback/laptop. |
| AUTOPLAY_BLOCKED | Browser refused scripted audio | Use tested operator activation once; then laptop. |
| PLAYER_ERROR | YouTube player rejected/failed video | Retry once or use approved alternate. |
| PLAYBACK_NOT_CONFIRMED | Command sent, player never reached PLAYING | Treat as failure; do not claim success. |
| FATAL_ERROR | Startup dependency failed | Laptop fallback. |

## 11. After the presentation

- Disarm/stop the microphone service.
- Shut down the Pi cleanly before removing power.
- If Google TV ADB was used, disable wireless debugging or unpair the Pi.
- Remove any temporary hotspot or travel-router configuration.
- Copy timing/result metrics if needed.
- Delete any explicitly recorded benchmark audio that no longer has a purpose.
- Do not copy API keys into presentation notes or chat.
- Record failures and environmental differences before changing the frozen build.

## 12. Native Google TV appendix

Only use this sequence if the ADB executor has separately passed `ACCEPTANCE_TESTS.md` in the same room.

Configuration:

```text
ReSpeaker -> Raspberry Pi on table
Google TV Streamer -> TV HDMI
Pi and Streamer -> approved private/local network
```

Additional preflight:

- Streamer power supply/cable passed one-hour soak.
- Pi reports ADB connected.
- YouTube TV package and version match the tested configuration.
- Deep link passed a rehearsal after both devices rebooted.
- TV is already on and Streamer input selected unless CEC passed 19/20 actual-room trials.
- Wireless debugging cleanup is assigned to an operator.

If any native-TV preflight item fails, use the Pi-direct runbook. Do not substitute Cast or directional UI automation live.
