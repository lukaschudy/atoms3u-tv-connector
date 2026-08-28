# Room Agent for iPhone

This native SwiftUI app replaces the hand-built personal Shortcut. It provides
the preconfigured **Talk to Room Agent** App Shortcut automatically, captures
one visible push-to-talk command, and sends only the transcript to the existing
authenticated Windows or Raspberry Pi gateway.

## User onboarding

1. The operator creates a ten-minute QR invitation on the Room Agent computer.
2. The user taps **Scan pairing QR** in Room Agent and scans it in the app.
3. Room Agent shows the computer name; the user taps **Connect**.
4. iOS asks for Microphone and Speech Recognition access on first use.
5. The app explains the final Apple-controlled step: **Settings → Action
   Button → Shortcut → Talk to Room Agent**.

Apple does not expose a supported API that lets an app assign the Action Button
itself. The App Shortcut is available immediately after installation, so no
shortcut authoring, URL, bearer token, UUID, or JSON editing is required.

## Create a pairing QR on Windows

After the updated Windows host is installed and running behind private
Tailscale Serve:

```powershell
cd "$env:USERPROFILE\atoms3u-tv-connector\room_agent"
.\.venv\Scripts\python.exe configure_iphone.py pair `
  --name "My iPhone" `
  --base-url "https://room-agent.example-tailnet.ts.net" `
  --qr-file "$env:TEMP\room-agent-pairing.svg"
```

Open the SVG on the PC and scan it inside Room Agent within ten minutes. The QR
contains a single-use temporary ticket, not the permanent device credential.
Successful redemption destroys the ticket; the app stores the issued token with
`kSecAttrAccessibleWhenUnlockedThisDeviceOnly` in Keychain.

## Generate the Xcode project

Native iOS apps must be compiled and signed on macOS. On a Mac with Xcode 26:

```bash
brew install xcodegen
cd room_agent/ios/RoomAgentApp
xcodegen generate
open RoomAgentApp.xcodeproj
```

In Xcode:

1. Select the **RoomAgentApp** target → **Signing & Capabilities**.
2. Select your Apple account's Personal Team and choose a unique bundle ID if
   `cz.miton.RoomAgent` is unavailable.
3. Connect the iPhone, trust the Mac, and enable Developer Mode when prompted.
4. Select the iPhone as the run destination and press **Run**.

A free Apple developer account can test directly on the owner's phone, but the
personal provisioning profile expires after seven days. TestFlight and durable
distribution require Apple Developer Program membership.

The repository also runs an unsigned Xcode 26 simulator build in GitHub Actions
for every iPhone-app pull request. That catches Apple SDK compile errors, but it
cannot sign or install the app on a physical phone.

## Security and privacy boundaries

- Pairing accepts a root Tailscale HTTPS origin ending in `.ts.net` (plus
  loopback-only developer builds) and a correctly shaped, one-use ticket.
- The server stores only hashes of pairing tickets and device tokens.
- Credentials are not placed in `UserDefaults`, logs, QR history, or source.
- The app records only while its listening UI is visible. It stops after 1.2
  seconds of speech silence or 12 seconds total.
- On-device speech recognition is required when the selected locale supports
  it; otherwise iOS speech recognition handles the audio under the explicit
  system permission.
- Only the recognized text is sent to Room Agent.
- Removing the connection deletes the local Keychain credential; the operator
  can independently revoke that device on the server.

## Current language choice

The app offers English (`en-US`) and Czech (`cs-CZ`). Apple's recognizer is
locale-specific, so v1 asks the user to choose rather than pretending language
detection is reliable. The Room Agent router already understands the bounded
English and Czech TV remote phrases.
