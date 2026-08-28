"""One-shot, allowlisted Windows voice-to-YouTube demonstration.

The program records from a ReSpeaker, transcribes locally, accepts only two
allowlisted intents (open YouTube or play Cinkatko), and opens the approved
target in the user's browser. Cinkatko metadata uses YouTube Data API v3.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
import wave
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SAMPLE_RATE = 16_000
DEFAULT_RECORDING_SECONDS = 7.0
YOUTUBE_API_ROOT = "https://www.googleapis.com/youtube/v3"


class DemoError(RuntimeError):
    """A user-facing, expected demo error."""


@dataclass(frozen=True)
class PlayIntent:
    action: str = "play"
    channel_key: str | None = "cinkatko"
    selector: str = "latest_episode"
    service: str = "youtube"


@dataclass(frozen=True)
class MediaItem:
    video_id: str
    title: str
    published_at: str
    duration_seconds: int
    source: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}&autoplay=1"


def normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def parse_intent(transcript: str) -> PlayIntent:
    text = normalize_text(transcript)
    words = set(text.split())

    action_words = {
        "open",
        "play",
        "plays",
        "pust",
        "puste",
        "pustit",
        "prehraj",
        "prehrajte",
        "spust",
        "spustit",
        "otevri",
    }
    channel_words = {"cinkatko", "cinkatka", "cinkatko", "miton", "mitonu"}

    if not words.intersection(action_words):
        raise DemoError("I heard speech, but not an approved play/open action.")
    if words.intersection(channel_words):
        return PlayIntent()
    if "youtube" in words:
        return PlayIntent(
            action="open",
            channel_key=None,
            selector="youtube_home",
        )

    raise DemoError("I heard an action, but it did not identify Cinkatko/Miton or YouTube.")


_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration(value: str) -> int:
    match = _DURATION_PATTERN.fullmatch(value)
    if not match:
        raise DemoError(f"Unsupported YouTube duration: {value!r}")
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _published_sort_value(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def select_latest_episode(
    videos: Iterable[dict[str, Any]], min_duration_seconds: int
) -> MediaItem:
    eligible: list[MediaItem] = []
    for video in videos:
        status = video.get("status", {})
        snippet = video.get("snippet", {})
        details = video.get("contentDetails", {})
        video_id = str(video.get("id", ""))
        duration_text = str(details.get("duration", ""))

        if not video_id or not duration_text:
            continue
        if status.get("privacyStatus") != "public" or status.get("embeddable") is False:
            continue
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
            continue

        duration = parse_iso8601_duration(duration_text)
        if duration < min_duration_seconds:
            continue

        eligible.append(
            MediaItem(
                video_id=video_id,
                title=str(snippet.get("title", "Untitled video")),
                published_at=str(snippet.get("publishedAt", "")),
                duration_seconds=duration,
                source="youtube_api",
            )
        )

    if not eligible:
        raise DemoError(
            f"No public, embeddable episode of at least {min_duration_seconds // 60} minutes was found."
        )
    return max(eligible, key=lambda item: _published_sort_value(item.published_at))


def _api_get(session: Any, resource: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = session.get(
            f"{YOUTUBE_API_ROOT}/{resource}", params=params, timeout=(3.0, 10.0)
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise DemoError(f"YouTube metadata request failed: {exc}") from exc
    if "error" in payload:
        raise DemoError(f"YouTube API returned an error: {payload['error']}")
    return payload


def resolve_latest_episode(
    api_key: str,
    handle: str,
    min_duration_seconds: int,
    session: Any | None = None,
) -> MediaItem:
    if not api_key:
        raise DemoError(
            "YOUTUBE_API_KEY is empty. Copy .env.example to .env and add the restricted API key."
        )

    if session is None:
        import requests

        session = requests.Session()

    channel_payload = _api_get(
        session,
        "channels",
        {
            "part": "contentDetails,snippet",
            "forHandle": handle,
            "key": api_key,
        },
    )
    channels = channel_payload.get("items", [])
    if len(channels) != 1:
        raise DemoError(f"Expected one channel for {handle}, received {len(channels)}.")

    try:
        uploads_playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError) as exc:
        raise DemoError(f"The uploads playlist was missing for {handle}.") from exc

    uploads_payload = _api_get(
        session,
        "playlistItems",
        {
            "part": "contentDetails",
            "playlistId": uploads_playlist,
            "maxResults": 15,
            "key": api_key,
        },
    )
    video_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in uploads_payload.get("items", [])
    ]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        raise DemoError(f"No public uploads were returned for {handle}.")

    videos_payload = _api_get(
        session,
        "videos",
        {
            "part": "contentDetails,snippet,status",
            "id": ",".join(video_ids),
            "key": api_key,
        },
    )
    return select_latest_episode(videos_payload.get("items", []), min_duration_seconds)


def list_input_devices() -> list[tuple[int, str, int]]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise DemoError("sounddevice is not installed. Run the installation command in README.md.") from exc

    result: list[tuple[int, str, int]] = []
    for index, device in enumerate(sd.query_devices()):
        channels = int(device.get("max_input_channels", 0))
        if channels > 0:
            result.append((index, str(device.get("name", "Unknown")), channels))
    return result


def choose_input_device(name_fragment: str) -> tuple[int, str, int]:
    candidates = [
        item for item in list_input_devices() if name_fragment.casefold() in item[1].casefold()
    ]
    if not candidates:
        available = "\n".join(
            f"  [{index}] {name} ({channels} input channel(s))"
            for index, name, channels in list_input_devices()
        )
        raise DemoError(
            f"No input device contains {name_fragment!r}. Available inputs:\n{available}"
        )
    return max(candidates, key=lambda item: item[2])


def record_audio(device_fragment: str, seconds: float) -> tuple[Any, str, int]:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise DemoError("Audio dependencies are not installed. Follow pc_demo/README.md.") from exc

    index, name, max_channels = choose_input_device(device_fragment)
    capture_channels = 6 if max_channels >= 6 else max(1, min(max_channels, 2))
    frames = int(seconds * SAMPLE_RATE)

    try:
        sd.check_input_settings(
            device=index,
            channels=capture_channels,
            dtype="float32",
            samplerate=SAMPLE_RATE,
        )
        recording = sd.rec(
            frames,
            samplerate=SAMPLE_RATE,
            channels=capture_channels,
            dtype="float32",
            device=index,
        )
        sd.wait()
    except Exception as exc:
        raise DemoError(f"Recording from {name!r} failed: {exc}") from exc

    if recording.size == 0:
        raise DemoError("The microphone returned an empty recording.")
    mono = np.ascontiguousarray(recording[:, 0], dtype=np.float32)
    peak = float(np.max(np.abs(mono)))
    if peak < 0.002:
        raise DemoError(
            f"The recording from {name!r} is effectively silent (peak {peak:.4f}). Check mute and input selection."
        )
    return mono, name, capture_channels


def save_wav(path: Path, audio: Any) -> None:
    import numpy as np

    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm.tobytes())


def transcribe_audio(
    audio: Any, model_name: str, language: str | None, use_vad: bool = True
) -> tuple[str, str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise DemoError("faster-whisper is not installed. Follow pc_demo/README.md.") from exc

    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            download_root=str(Path(__file__).with_name(".models")),
        )
        if language == "en":
            initial_prompt = "Open YouTube on the laptop. YouTube, Cinkatko, Miton."
        elif language == "cs":
            initial_prompt = "Otevři YouTube na notebooku. Pusť Cinkátko od Mitonu."
        else:
            initial_prompt = "Open YouTube. Otevři YouTube. Cinkátko, Miton."
        segments, info = model.transcribe(
            audio,
            language=language,
            beam_size=5,
            vad_filter=use_vad,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        raise DemoError(f"Local speech recognition failed: {exc}") from exc

    if not transcript:
        raise DemoError("No speech was recognized.")
    return transcript, str(getattr(info, "language", language or "unknown"))


def fallback_media(video_id: str) -> MediaItem:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise DemoError("FALLBACK_VIDEO_ID must be an 11-character YouTube video ID.")
    return MediaItem(
        video_id=video_id,
        title="Configured fallback video",
        published_at="",
        duration_seconds=0,
        source="configured_fallback",
    )


def load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).with_name(".env"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true", help="List microphone inputs and exit.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Use supplied text instead of recording/transcribing.")
    source.add_argument("--audio-file", type=Path, help="Transcribe an existing audio file.")
    parser.add_argument(
        "--transcribe-only", action="store_true", help="Stop after printing the transcript."
    )
    parser.add_argument("--parse-only", action="store_true", help="Stop after validating the transcript.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve but do not open the browser.")
    parser.add_argument("--seconds", type=float, default=DEFAULT_RECORDING_SECONDS)
    parser.add_argument("--device", help="Case-insensitive microphone name fragment.")
    parser.add_argument("--model", help="Multilingual faster-whisper model name.")
    parser.add_argument("--language", help="Language code such as cs, en, or auto.")
    parser.add_argument(
        "--no-vad", action="store_true", help="Disable silence filtering for audio diagnostics."
    )
    parser.add_argument("--save-audio", type=Path, help="Optional path for the mono WAV recording.")
    return parser


def run(args: argparse.Namespace) -> int:
    load_environment()

    if args.list_devices:
        for index, name, channels in list_input_devices():
            print(f"[{index}] {name} ({channels} input channel(s))")
        return 0

    if args.seconds <= 0 or args.seconds > 30:
        raise DemoError("--seconds must be greater than 0 and no more than 30.")

    if args.text:
        transcript = args.text
        language = "provided text"
    elif args.audio_file:
        if not args.audio_file.is_file():
            raise DemoError(f"Audio file does not exist: {args.audio_file}")
        configured_language = args.language or os.getenv("STT_LANGUAGE", "auto")
        stt_language = None if configured_language.casefold() == "auto" else configured_language
        model_name = args.model or os.getenv("WHISPER_MODEL", "base")
        print(f"Transcribing existing audio locally with multilingual Whisper {model_name!r}...")
        transcript, language = transcribe_audio(
            str(args.audio_file), model_name, stt_language, use_vad=not args.no_vad
        )
    else:
        input("Press Enter, then speak the complete command... ")
        print(f"Listening for {args.seconds:g} seconds...")
        audio, device_name, channels = record_audio(
            args.device or os.getenv("AUDIO_DEVICE", "respeaker"), args.seconds
        )
        print(f"Captured {channels} channel(s) from: {device_name}; using processed channel 0.")
        if args.save_audio:
            save_wav(args.save_audio, audio)
            print(f"Saved mono recording: {args.save_audio}")

        configured_language = args.language or os.getenv("STT_LANGUAGE", "auto")
        stt_language = None if configured_language.casefold() == "auto" else configured_language
        model_name = args.model or os.getenv("WHISPER_MODEL", "base")
        print(f"Transcribing locally with multilingual Whisper {model_name!r}...")
        transcript, language = transcribe_audio(
            audio, model_name, stt_language, use_vad=not args.no_vad
        )

    print(f"Transcript ({language}): {transcript}")
    if args.transcribe_only:
        return 0
    intent = parse_intent(transcript)
    if intent.selector == "youtube_home":
        print("Accepted intent: open the YouTube homepage")
    else:
        print(f"Accepted intent: {intent.selector} from {intent.channel_key} on {intent.service}")
    if args.parse_only:
        return 0

    if intent.selector == "youtube_home":
        url = "https://www.youtube.com/"
        print(f"URL: {url}")
        if args.dry_run:
            print("Dry run: browser was not opened.")
            return 0
        if not webbrowser.open(url, new=2, autoraise=True):
            raise DemoError("Windows did not accept the request to open the browser.")
        print("YouTube was opened in the default browser.")
        return 0

    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    try:
        media = resolve_latest_episode(
            api_key=api_key,
            handle=os.getenv("CINKATKO_HANDLE", "@mitoncz"),
            min_duration_seconds=int(os.getenv("MIN_EPISODE_SECONDS", "600")),
        )
    except DemoError:
        fallback_id = os.getenv("FALLBACK_VIDEO_ID", "").strip()
        if not fallback_id:
            raise
        media = fallback_media(fallback_id)
        print("Live lookup failed; using the explicitly configured fallback video.")

    print(f"Selected: {media.title}")
    print(f"Source: {media.source}")
    print(f"URL: {media.url}")
    if args.dry_run:
        print("Dry run: browser was not opened.")
        return 0

    if not webbrowser.open(media.url, new=2, autoraise=True):
        raise DemoError("Windows did not accept the request to open the browser.")
    print("The approved YouTube video was opened in the default browser.")
    return 0


def main() -> int:
    # Windows terminals may inherit a legacy code page that cannot print Czech
    # characters even though the transcript itself is valid Unicode.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    try:
        return run(build_parser().parse_args())
    except (DemoError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
