# Production iPhone Action Button setup

The iPhone is a private push-to-talk remote for the same Room Agent that listens
through the ReSpeaker. Holding the Action Button starts Apple's `Dictate Text`
interface, and the Shortcut sends the transcript to the Pi. The Pi remains the
only router and executor; the iPhone receives no TV credentials or broad tool
access.

This is not a replacement for Siri's always-listening system integration. iOS
allows a third-party Shortcut to run from the Action Button, but iOS owns the
microphone and permission prompts.

## Production topology

```text
iPhone Action Button
  -> Apple Dictation
  -> private HTTPS URL on Tailscale
  -> loopback-only iPhone gateway on the Pi
  -> shared serialized Room Agent session
  -> native smart-TV endpoint or Pi HDMI fallback

ReSpeaker -> local Whisper ----------------------^
```

Tailscale is the production transport. It keeps port 8770 off the LAN and public
internet, provides an HTTPS certificate, and lets the Shortcut work away from
the office as long as the iPhone and Pi are online. Do not use Tailscale Funnel;
the gateway must remain private to the tailnet.

## 1. Install the production service

Copy the updated project to `/home/voiceagent/room-agent`, then replace the user
service:

```bash
cd ~/room-agent
mkdir -p ~/.config/systemd/user
cp room-agent.service ~/.config/systemd/user/room-agent.service

# Remove the superseded iPhone-only service if the prototype installed it.
systemctl --user disable --now room-agent-iphone.service 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now room-agent.service
systemctl --user status room-agent.service --no-pager
```

The service now runs `--hands-free --iphone-gateway`. One in-process lock
serializes commands, so an iPhone and the ReSpeaker cannot manipulate Chromium
or a native TV endpoint at the same time. Their commands do share the active
target: starting playback with one input and pausing it with the other works.

The gateway listens only on `127.0.0.1:8770`. Its service files use a private
umask, automatic restart, and a bounded shutdown.

## 2. Add the Pi and iPhone to Tailscale

Install Tailscale on the Pi using its current official Debian instructions and
sign it into a dedicated tailnet. Install the Tailscale iOS app on the iPhone,
approve its VPN configuration, and sign into the same tailnet. For a corporate
deployment, use a company-owned tailnet, MFA, device approval, and an ACL that
allows this iPhone to reach only this Pi.

On the Pi, publish only the loopback gateway through private Tailscale Serve:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8770
tailscale serve status
```

The status output shows an address similar to:

```text
https://room-agent.example-tailnet.ts.net
```

Verify it in Safari on the iPhone while Tailscale is connected:

```text
https://room-agent.example-tailnet.ts.net/health
```

The response should contain `"status":"ok"`. Tailscale Serve persists its
background configuration across reboots. If the company cannot approve
Tailscale, put an authenticated corporate HTTPS reverse proxy in front of the
same loopback port; never bind the production gateway to `0.0.0.0` or forward
port 8770 from a router.

## 3. Enrol the iPhone

Create one revocable identity for this phone:

```bash
cd ~/room-agent
python3 configure_iphone.py enroll \
  --name "My iPhone" \
  --base-url "https://room-agent.example-tailnet.ts.net" \
  --show-token
```

The command prints the command URL and one `Authorization: Bearer <token>` value.
The raw credential is displayed once. The Pi stores only its SHA-256 hash in
`~/.config/room-agent/iphone-devices.json`, with user-only permissions.

Useful lifecycle commands:

```bash
python3 configure_iphone.py list
python3 configure_iphone.py rotate DEVICE_ID \
  --base-url "https://room-agent.example-tailnet.ts.net" \
  --show-token
python3 configure_iphone.py revoke DEVICE_ID
```

Rotation immediately invalidates the previous token. Revocation takes effect
without restarting Room Agent. Use a separate enrollment for every iPhone;
never share one bearer credential between people.

## 4. Build the Room Agent Shortcut

In **Shortcuts**, create a shortcut named **Room Agent** with these actions in
this order:

1. **Dictate Text**. Set `Stop Listening` to `After Pause` and choose the
   preferred language.
2. **Generate UUID**. This is the request identifier and prevents an HTTP retry
   from executing the same command twice.
3. **URL** -> paste the `/v1/commands` URL printed during enrollment.
4. **Get Contents of URL**:
   - Method: `POST`
   - Request Body: `JSON`
   - Add `version` as a Number with value `1`
   - Add `request_id` as Text with the **UUID** magic variable
   - Add `text` as Text with the **Dictated Text** magic variable
   - Header `Authorization`: paste the complete `Bearer ...` value
5. **Get Dictionary Value** -> key `popup` from **Contents of URL**.
6. **Vibrate Device**.
7. **Show Notification** with the Dictionary Value from step 5. The server
   formats a native-looking multiline banner with an action icon, concise
   result, selected device, and execution time. Do not use **Show Result** for
   the Action Button flow: its modal sheet can block the next voice command
   until dismissed. Keep Shortcut error reporting enabled so authentication,
   network, and routing failures remain visible.

In **Settings -> Action Button**, swipe to **Shortcut**, choose **Room Agent**,
then select the shortcut. Run it once with the iPhone unlocked and accept the
microphone, speech-recognition, Shortcuts networking, and Tailscale prompts.

The credential is intentionally inside a private Shortcut. Do not publish the
Shortcut, paste it into chat, or include it in screenshots. If it is exposed,
rotate the enrollment rather than editing the registry by hand.

## 5. Acceptance test

With the ReSpeaker and iPhone both active, run this exact sequence:

1. ReSpeaker: `Open YouTube on the TV.`
2. iPhone Action Button: `Pause the video.`
3. iPhone Action Button: `Continue.`
4. ReSpeaker: `Close the tab.`
5. iPhone Action Button: `Play the latest Cinkatko podcast on the TV.`

Then test the failure cases:

- Turn Tailscale off on the iPhone: the Shortcut must fail and the TV must not
  change.
- Submit the same request UUID twice: the second response contains
  `"replayed":true` and the command runs only once.
- Rotate the phone token: the old Shortcut must receive HTTP 401.
- Restart the Pi: the room-agent service and Tailscale Serve must return without
  an SSH login.
- Disconnect the ReSpeaker: iPhone commands must continue working; reconnecting
  the microphone must restore local voice capture.

The gateway accepts at most 12 new commands per enrolled iPhone per minute,
limits request size and transcript length, rejects additional model-supplied
fields, and caches request IDs for ten minutes. Its metadata-only audit trail is
at `~/.local/state/room-agent/iphone-audit.jsonl`; transcripts and bearer tokens
are not logged. The service journal also redacts transcripts unless an operator
explicitly starts the hub with `--log-transcripts` for a supervised diagnosis.

## Operations

```bash
systemctl --user is-active room-agent.service room-agent-hermes.service
curl -fsS http://127.0.0.1:8770/health
tailscale serve status
journalctl --user -u room-agent.service -n 100 --no-pager
tail -n 20 ~/.local/state/room-agent/iphone-audit.jsonl
```

Back up the source and non-secret display configuration, but do not back up or
commit the iPhone registry, audit log, `.env`, TV credentials, or bearer token.
