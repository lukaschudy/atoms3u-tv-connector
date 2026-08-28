# Voice Demo — One-Page Decision Summary

## Recommended boss demo

Use the Raspberry Pi as both the voice agent and playback computer:

```text
ReSpeaker -> Raspberry Pi 5 -> HDMI -> TV
```

Turn the TV on and select the Pi input before the presentation. The Pi boots into a full-screen status/player UI, waits for one valid utterance, resolves the latest full-length Cinkátko episode, plays it through the official YouTube player, confirms playback, and disarms.

Describe this accurately as voice-controlled playback on a TV display. Native control of the TV/Google TV operating system is the second, ADB-based experiment.

Develop and rehearse the same stack on a Windows laptop. Keep the laptop as the tested demo-day fallback.

## Why this route

- One playback computer instead of Pi plus Streamer.
- No ADB, Cast, mDNS, peer-to-peer LAN, client-isolation, or WPA2-Enterprise dependency.
- No wake word, LLM, speaker, SSD, AI accelerator, or active cooler needed for the first version.
- The exact same voice/content modules can later drive the Google TV Streamer through a separate executor.

## Product decisions

- “Latest Cinkátko” means newest playable full-length episode, initially duration >= 10 minutes, not necessarily the literal newest Short/teaser.
- The known channel is configured as `@mitoncz`; it is not rediscovered by free-form search on each command.
- Local multilingual whisper.cpp is tested first; cloud STT is considered only if the actual-room accuracy/latency gate fails.
- Initial interaction is one-shot: READY -> command -> PLAYING -> DISARMED.
- YouTube metadata comes from the official Data API; playback uses the official player.
- The system never bypasses YouTube ads. Use a company-approved Premium demo profile if predictable presentation playback is required.
- CEC power/input control is optional and tested later against the exact room AV chain.

## What may actually be missing

- A room-length HDMI cable or USB data extension so the microphone can sit on the table.
- A power strip/extension lead.
- A suitable Internet connection for the Pi.
- A company-approved demo Google/YouTube account, preferably Premium.
- If using Google TV later: a stable 5 V/1.5 A supply/cable and an IT-approved network where Pi and Streamer can communicate.
- Exact TV model and knowledge of whether HDMI passes through an AVR, wall plate, extender, or matrix.

No additional RAM, speaker, microSD reader, SSD, USB hub, HDMI switch, active cooler, or AI accelerator is currently justified.

## Build order

1. Inspect the room, cabling, TV, network, and Streamer power.
2. Build a Windows vertical slice: ReSpeaker -> STT -> typed intent -> YouTube resolver -> full-screen player.
3. Port the same stack to Raspberry Pi OS 64-bit and configure supervised backend plus Chromium kiosk startup.
4. Hard-test audio placement, autoplay, cache fallback, thermals, reboot recovery, and invalid speech.
5. Freeze versions and rehearse the laptop fallback.
6. Only then add the Google TV ADB executor.

If a later requirement is for the Pi to control a separate Windows PC, add an authenticated allowlisted companion service. Do not turn recognized speech into arbitrary remote PowerShell or mouse-coordinate automation.

## Boss-demo gate

Do not present until the final packed hardware achieves:

- At least 19 correct launches in 20 dry runs in the actual room.
- Zero incorrect-video launches.
- Median end-of-command to confirmed playback <= 4 seconds; P95 <= 7 seconds.
- Zero false launches during a 30-minute TV/background-noise soak.
- Unattended boot to READY.
- Explicit playback confirmation, including autoplay-blocked detection.
- Verified API cache fallback.
- Rehearsed laptop recovery in under five minutes.

## Documents

- `PROJECT_PLAN.md` — architecture, modules, decisions, phases, risks, and references.
- `ACCEPTANCE_TESTS.md` — objective unit, hardware, room, kiosk, and ADB gates.
- `DEMO_RUNBOOK.md` — packing, preflight, presentation sequence, and failure ladder.

## Inputs needed next

1. Exact room TV/display model.
2. Direct HDMI versus corporate AV path.
3. Table-to-HDMI distance.
4. Whether native Google TV UI is essential to the story.
5. Corporate network constraints and hotspot/router policy.
6. Local-only versus approved cloud speech processing.
7. Czech-only or Czech plus English commands.
8. Availability of a demo YouTube Premium account and a rights-cleared local fallback clip.
