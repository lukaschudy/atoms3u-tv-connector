# Windows PC voice-control checkpoint

This is a deliberately small, one-shot test of the complete logical path:

```text
ReSpeaker -> local speech recognition -> allowlisted intent -> YouTube metadata -> browser
```

It cannot run arbitrary commands. It accepts two allowlisted actions:

- Open the YouTube homepage.
- Play the latest eligible Cinkatko/Miton episode.

## 1. Connect and check the microphone

Connect the ReSpeaker directly to the laptop with its USB data cable. In Windows Sound settings, make sure it appears as an input device and is not muted.

## 2. Create the isolated Python environment

Open PowerShell in this folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first install downloads the local speech-recognition runtime. No OpenAI or other speech API key is required.

## 3. Add the YouTube API key

In Google Cloud Console, enable **YouTube Data API v3**, create an API key, and restrict the key to that API.

Then create the local configuration:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `YOUTUBE_API_KEY` in `.env`. Do not paste the key into source code or commit `.env`.

## 4. Verify the microphone selection

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --list-devices
```

At least one input should contain `ReSpeaker`. The program automatically prefers the matching endpoint with the greatest available channel count.

## 5. Check parsing without the microphone or browser

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --text "Pusť poslední díl Cinkátka na YouTube" --parse-only
```

Expected result:

```text
Accepted intent: latest_episode from cinkatko on youtube
```

## 6. Check the live YouTube lookup without opening it

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --text "Pusť poslední díl Cinkátka na YouTube" --dry-run
```

Confirm that the title and URL identify the newest eligible full-length episode.

## 7. Run the voice demonstration

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --save-audio last-command.wav
```

Press Enter and say one complete command, for example:

> Pusť poslední díl Cinkátka na YouTube.

For the basic PC-control checkpoint, this also works without a YouTube API key:

> Open YouTube.

The first run downloads the multilingual Whisper model and therefore takes longer. Later runs reuse the local model. The default is CPU `int8`, so no CUDA setup is required.

The program records for seven seconds, transcribes locally, validates the narrow command, resolves the episode, and opens it in the default browser. If the browser does not autoplay the first time, allow autoplay for YouTube; the dedicated kiosk version will use an explicit autoplay policy.

## Useful options

Transcribe a saved recording without taking an action:

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --audio-file .\last-command.wav --transcribe-only --language en
```

Use a different microphone-name fragment:

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --device "Microphone Array"
```

Force Czech or English recognition:

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --language cs
.\.venv\Scripts\python.exe voice_pc_demo.py --language en
```

Try a faster but less accurate model:

```powershell
.\.venv\Scripts\python.exe voice_pc_demo.py --model tiny
```

## Tests

The parser and episode-selection policy use only the Python standard library:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

The tests confirm Czech normalization, Czech/English commands, rejection of unrelated PC actions, ISO-8601 durations, and skipping a Short in favor of the newest full-length episode.
