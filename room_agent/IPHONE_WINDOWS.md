# iPhone Action Button with the Windows host

This is the recommended setup when the notebook is present in the room. The
iPhone app sends recognized text directly to one lightweight Windows process.
That process runs the existing deterministic/DeepSeek router, YouTube resolver,
and a separately tracked window in the normal signed-in Brave profile. The
Raspberry Pi is not required.

```text
iPhone Action Button -> Apple Shortcut or Room Agent app -> private Tailscale HTTPS
  -> Windows voice bridge -> signed local Wi-Fi command
  -> ATOMS3U -> IR / USB HID -> TV
```

Complete the connector preparation and `connect-host` steps in the
[root quick start](../README.md) before installing this bridge. With an enrolled
ATOMS3U, unqualified commands target the TV. Explicit **on the laptop** commands
use a separately tracked Brave window without closing existing personal tabs.

YouTube is conversational. After **Open YouTube**, a follow-up such as **Play
Never Gonna Give You Up** stays with the same TV session. An IR-only profile can
send remote keys but cannot type an arbitrary title. App launch and text search
require USB HID support, the LG hybrid profile, or a separately paired native
smart-TV adapter. Unsupported requests are rejected instead of guessed.

## 1. Test and install the Windows host

From PowerShell:

```powershell
cd "$env:USERPROFILE\atoms3u-tv-connector\room_agent"
python windows_iphone_host.py --personal-browser-profile --text "Open YouTube"
```

This should open a new window in normal Brave. Close it, then install
the background task:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows-host.ps1
```

The installer creates a private `.venv`, registers one restartable per-user
Scheduled Task, starts it, and verifies `http://127.0.0.1:8770/health`. The task
runs only while this Windows user is signed in, which is necessary for opening
an interactive browser. It is one Python process; the virtual environment itself
does not consume memory.

To remove startup later without deleting enrollments:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows-host.ps1 -Remove
```

## 2. Connect Windows and iPhone privately

Install the official Tailscale client on Windows and iPhone, sign both into the
same private tailnet, and show **Connected** on both. Do not use Funnel.

Open **PowerShell as Administrator** on Windows and publish only the loopback
gateway:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8770
tailscale serve status
```

The status output provides a private address such as:

```text
https://room-agent.example-tailnet.ts.net
```

Tailscale terminates HTTPS and preserves this Serve configuration across
reboots. Port 8770 remains unavailable directly from the office LAN and public
internet.

## 3. Pair the native Room Agent app

The native app removes the manual Shortcut, bearer-token, URL, UUID and JSON
setup. Its source and Mac installation guide are in
[`ios/RoomAgentApp`](ios/RoomAgentApp).

Once the app is installed, create a ten-minute, one-use QR invitation:

```powershell
cd "$env:USERPROFILE\atoms3u-tv-connector\room_agent"
.\.venv\Scripts\python.exe configure_iphone.py pair `
  --name "My iPhone" `
  --base-url "https://room-agent.example-tailnet.ts.net" `
  --qr-file "$env:TEMP\room-agent-pairing.svg"
Invoke-Item "$env:TEMP\room-agent-pairing.svg"
```

In Room Agent on the iPhone, tap **Scan pairing QR**, scan the screen and
confirm the displayed HTTPS computer address. The QR contains only a temporary
ticket. The permanent device credential is returned over HTTPS and stored in
iOS Keychain.

The app automatically publishes **Talk to Room Agent** in App Shortcuts. Apple
requires one final user-controlled choice: open **Settings -> Action Button ->
Shortcut** and select **Talk to Room Agent**. The app includes this guide and a
button that opens its Shortcuts page.

Native iOS software must be compiled and signed with Xcode on macOS. A free
Personal Team is sufficient for a direct test on the owner's iPhone, but that
test installation expires after seven days. TestFlight distribution requires
the Apple Developer Program.

## 4. Manage or revoke phones

Windows stores only token hashes in
`%LOCALAPPDATA%\RoomAgent\iphone-devices.json`. List or revoke a phone with:

```powershell
.\.venv\Scripts\python.exe configure_iphone.py list
.\.venv\Scripts\python.exe configure_iphone.py revoke DEVICE_ID
```

## 5. Legacy manual Shortcut fallback

Use the HTTPS address from `tailscale serve status`:

```powershell
cd "$env:USERPROFILE\atoms3u-tv-connector\room_agent"
python configure_iphone.py enroll `
  --name "My iPhone" `
  --base-url "https://room-agent.example-tailnet.ts.net" `
  --show-token
```

The raw credential is displayed once. Windows stores only its hash in
`%LOCALAPPDATA%\RoomAgent\iphone-devices.json`. Rotate or revoke a phone with:

```powershell
python configure_iphone.py list
python configure_iphone.py rotate DEVICE_ID `
  --base-url "https://room-agent.example-tailnet.ts.net" `
  --show-token
python configure_iphone.py revoke DEVICE_ID
```

Create the manual Action Button Shortcut only when the native app is not being
used:

Create a Shortcut called **Room Agent**:

1. **Dictate Text** -> stop listening `After Pause`.
2. **Current Date** (used as a unique identifier for this command).
3. **URL** -> paste the printed `/v1/commands` URL.
4. **Get Contents of URL** -> `POST`, request body `JSON`:
   - `version` -> Number -> `1`
   - `request_id` -> Text -> **Current Date** magic variable
   - `text` -> Text -> **Dictated Text** magic variable
   - Header `Authorization` -> paste the complete printed `Bearer ...` value
5. **Get Dictionary Value** -> `popup` from **Contents of URL**.
6. **Vibrate Device**.
7. **Show Notification** -> use the Dictionary Value from step 5. This produces
   a compact native banner such as:

   ```text
   ▶️  Now playing
   The newest podcast episode
   Television  •  1.8 s
   ```

   Prefer **Show Notification** over **Show Result**: a result is modal and can
   block the next Action Button command until dismissed. Leave Shortcut error
   reporting enabled. The response still includes the original structured JSON
   plus a `shortcut_card` dictionary for a future app.

Run it once while unlocked and accept microphone, speech recognition, network,
and Tailscale prompts. Then choose **Room Agent** under **Settings -> Action
Button -> Shortcut**.

## 6. Acceptance sequence

1. `Open YouTube.`
2. `Play Never Gonna Give You Up.`
3. `Close tab.`
4. `Open YouTube.`
5. `Play the latest Cinkatko podcast.`
6. `Pause the video.`
7. `Continue.`
8. `Close tab.`
9. `Google how much an elephant weighs.`
10. `Open the first result.`

Also verify that turning Tailscale off prevents execution, rotating the token
invalidates the old Shortcut, and signing out/restarting Windows brings the
Scheduled Task and health endpoint back automatically.

The current Windows host controls the notebook or an attached HDMI display. A
later optional TV bridge can forward typed `tv` actions to a Pi or native smart
TV endpoint without changing the iPhone Shortcut.
