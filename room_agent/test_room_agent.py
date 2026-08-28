import json
import hashlib
import hmac
import os
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from agent_state import AgentState, InvalidStateTransition, StateEvent, StateMachine
from configure_hermes import (
    HermesConfigurationError,
    configure_hermes,
    read_env_values,
)
from configure_connector import (
    configure_ble_host,
    configure_firmware_profile,
    configure_wifi_host,
    discover_connectors,
    main as configure_connector_main,
    provision_firmware_network,
    sync_firmware_key,
)
from configure_display import read_config as read_display_config
from configure_display import remove_endpoint, upsert_endpoint
from configure_iphone import _pairing_link, _print_shortcut_values, _save_qr
from connector_simulator import (
    VirtualTvState,
    build_parser as build_connector_simulator_parser,
    make_connector_simulator_handler,
)
from display_manager import (
    CAP_APP_NETFLIX,
    CAP_APP_YOUTUBE,
    CAP_MEDIA_CLOSE,
    CAP_MEDIA_PAUSE,
    CAP_MEDIA_RESUME,
    CAP_WEB_BROWSER,
    CAP_WEB_SEARCH,
    CAP_YOUTUBE_PLAY,
    AndroidTvEndpoint,
    AppleTvEndpoint,
    DisplayControlError,
    DisplayEndpoint,
    DisplayManager,
    EndpointStatus,
    LgWebOsEndpoint,
    SamsungTvEndpoint,
    load_display_manager,
)
from lg_webos import discover_lg_webos
from samsung_tizen import SamsungTizenError, _allowed_web_url, _compact_apps
from tv_connector import (
    BLE_FRAME_DATA,
    BLE_FRAME_END,
    BLE_FRAME_START,
    BleTvConnectorClient,
    CONNECTOR_KEYS_BY_PROFILE,
    CONNECTOR_VERSION,
    ConnectorPlan,
    ConnectorStep,
    DEFAULT_CONNECTOR_PROFILE,
    LG_WEBOS_HYBRID_PROFILE,
    LG_TV_IR_PROFILE,
    MAX_CONNECTOR_BODY_BYTES,
    SONY_BRAVIA_IR_PROFILE,
    SUPPORTED_CONNECTOR_PROFILES,
    TvConnectorClient,
    TvConnectorError,
    UsbTvConnectorClient,
    build_ble_frames,
)
from hermes_llm import (
    HermesLlmError,
    HermesLlmRouter,
    request_hermes_json,
)
from iphone_gateway import (
    IdempotencyStore,
    IphoneCommandServer,
    IphoneDeviceRegistry,
    IphoneGatewayError,
    SlidingWindowRateLimiter,
    add_shortcut_presentation,
    make_iphone_handler,
    parse_command,
    parse_transcript,
)
from intent_router import NoActivation, NoRoute, route
from local_llm import (
    FallbackLlmRouter,
    LocalLlmError,
    LocalLlmRouter,
    hybrid_route,
)
from openrouter_llm import (
    OPENROUTER_ENDPOINT,
    OpenRouterLlmError,
    OpenRouterLlmRouter,
    request_openrouter_json,
)
from pi_hub import (
    CommandOutcome,
    CommandSession,
    HubError,
    build_semantic_router,
    dispatch,
    execute_local_tv_action,
    hands_free_commands,
    prompt_text_console,
)
from pi_player import ChromiumHdmiPlayer, PiPlayerError
from protocol import (
    ActionRequest,
    ProtocolError,
    generate_key,
    read_key,
    sign_response,
    signed_headers,
    verify_headers,
    verify_response,
    write_private_text,
)
from respeaker_led import (
    COMMAND_BRIGHTNESS,
    COMMAND_CUSTOM,
    COMMAND_LISTEN,
    COMMAND_MONO_COLOR,
    ReSpeakerLedObserver,
)
from respeaker_tuning import ReSpeakerTuning, TuningUnavailable
from room_canvas import RoomCanvasServer, RoomCanvasState
from windows_browser import WindowsBrowserController, WindowsBrowserError
from windows_companion import ActionError, execute_action, make_handler
from windows_iphone_host import WindowsHostError, WindowsIphoneSession
from youtube_resolver import (
    MediaItem,
    PodcastCatalog,
    YouTubeResolverError,
    load_podcast_catalog,
    parse_iso8601_duration,
    resolve_latest_episode,
    resolve_latest_search,
    resolve_video_search,
    select_latest_episode,
)


TEST_CONNECTOR_BOOT_ID = "11" * 16


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.secret = bytes.fromhex("11" * 32)
        self.action = ActionRequest.create("web.open", "notebook", {"site": "youtube"})
        self.body = self.action.to_bytes()

    def test_round_trip(self):
        decoded = ActionRequest.from_bytes(self.body)
        self.assertEqual(decoded, self.action)

    def test_signature_and_replay_protection(self):
        headers = signed_headers(self.secret, self.body, now=1_000)
        seen = {}
        verify_headers(self.secret, self.body, headers, seen, now=1_000)
        with self.assertRaises(ProtocolError):
            verify_headers(self.secret, self.body, headers, seen, now=1_000)

    def test_replay_expiry_covers_the_complete_signed_timestamp_window(self):
        future_headers = signed_headers(self.secret, self.body, now=1_029)
        future_seen = {}
        verify_headers(self.secret, self.body, future_headers, future_seen, now=1_000)
        future_nonce = future_headers["X-Room-Agent-Nonce"]
        self.assertEqual(future_seen[future_nonce], 1_059)
        with self.assertRaisesRegex(ProtocolError, "already been used"):
            verify_headers(self.secret, self.body, future_headers, future_seen, now=1_031)

        past_headers = signed_headers(self.secret, self.body, now=971)
        past_seen = {}
        verify_headers(self.secret, self.body, past_headers, past_seen, now=1_000)
        self.assertEqual(past_seen[past_headers["X-Room-Agent-Nonce"]], 1_001)

    def test_tampering_is_rejected(self):
        headers = signed_headers(self.secret, self.body, now=1_000)
        with self.assertRaises(ProtocolError):
            verify_headers(self.secret, self.body + b" ", headers, {}, now=1_000)

    def test_response_signature_rejects_tampering(self):
        body = b'{"status":"executed"}'
        signature = sign_response(self.secret, body)

        verify_response(self.secret, body, signature)
        with self.assertRaisesRegex(ProtocolError, "response signature"):
            verify_response(self.secret, body + b" ", signature)

    def test_response_signature_matches_firmware_contract_vector(self):
        body = b'{"status":"executed"}'

        signature = sign_response(bytes.fromhex("01" * 32), body)

        self.assertEqual(
            signature,
            "fb027e47b57497d29df39b04c59f87c28efdf1625e5ac9b7ee5b2c1d964c5ed8",
        )

    def test_key_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pairing.key"
            generate_key(path)
            self.assertEqual(len(read_key(path)), 32)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_key_read_rejects_symlinks_even_during_permission_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target.key"
            target.write_text("ab" * 32, encoding="ascii")
            link = Path(temp) / "pairing.key"
            link.symlink_to(target)

            with self.assertRaisesRegex(ProtocolError, "pairing key"):
                read_key(link, require_private=False)


class DisplayConfigurationTests(unittest.TestCase):
    def test_enrollment_upserts_by_id_and_can_be_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "display_endpoints.json"
            endpoint = {
                "id": "google_tv",
                "name": "Google TV",
                "kind": "android_tv",
                "priority": 100,
                "enabled": True,
                "serial": "10.0.0.30:5555",
                "host": "10.0.0.30",
                "adb": "adb",
                "connect_on_probe": True,
            }
            upsert_endpoint(path, endpoint)
            upsert_endpoint(path, {**endpoint, "name": "Boardroom Google TV"})

            payload = read_display_config(path)
            removed = remove_endpoint(path, "google_tv")

            self.assertEqual(len(payload["endpoints"]), 1)
            self.assertEqual(payload["endpoints"][0]["name"], "Boardroom Google TV")
            self.assertTrue(removed)
            self.assertEqual(read_display_config(path)["endpoints"], [])


class HermesConfigurationTests(unittest.TestCase):
    def test_configurator_reuses_openrouter_key_without_printing_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            room_dir = root / "room-agent"
            room_dir.mkdir(parents=True)
            (room_dir / ".env").write_text(
                "YOUTUBE_API_KEY=test-youtube\n"
                "OPENROUTER_API_KEY=sk-or-private-test\n",
                encoding="utf-8",
            )

            result = configure_hermes(
                home=home,
                room_dir=room_dir,
                secret_factory=lambda _length: "ab" * 32,
            )

            hermes_values = read_env_values(result["hermes_env"])
            room_values = read_env_values(result["room_env"])
            self.assertEqual(hermes_values["OPENROUTER_API_KEY"], "sk-or-private-test")
            self.assertEqual(hermes_values["API_SERVER_KEY"], "ab" * 32)
            self.assertEqual(room_values["HERMES_API_KEY"], "ab" * 32)
            self.assertEqual(room_values["YOUTUBE_API_KEY"], "test-youtube")
            self.assertTrue(result["config"].is_file())
            self.assertTrue(result["unit"].is_file())
            self.assertTrue(result["room_unit"].is_file())

    def test_configurator_refuses_mismatched_existing_sidecar_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            room_dir = root / "room-agent"
            room_dir.mkdir(parents=True)
            (room_dir / ".env").write_text(
                "OPENROUTER_API_KEY=sk-or-private-test\n"
                f"HERMES_API_KEY={'a' * 32}\n",
                encoding="utf-8",
            )
            hermes_home = home / ".hermes"
            hermes_home.mkdir(parents=True)
            (hermes_home / ".env").write_text(
                f"API_SERVER_KEY={'b' * 32}\n",
                encoding="utf-8",
            )

            with self.assertRaises(HermesConfigurationError):
                configure_hermes(home=home, room_dir=room_dir)

class RouterTests(unittest.TestCase):
    def test_english_route(self):
        action = route("Open YouTube on the laptop")
        self.assertEqual(action.action, "web.open")
        self.assertEqual(action.target, "notebook")

    def test_open_youtube_can_default_to_local_tv(self):
        action = route("Open YouTube", default_open_target="tv")
        self.assertEqual(action.action, "web.open")
        self.assertEqual(action.target, "tv")

    def test_explicit_laptop_overrides_local_tv_default(self):
        action = route(
            "Open YouTube on the laptop",
            default_open_target="tv",
        )
        self.assertEqual(action.target, "notebook")

    def test_open_miton_routes_to_allowlisted_local_site(self):
        action = route("Open miton.cz", default_open_target="tv")
        self.assertEqual(action.action, "web.open")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"site": "miton"})

    def test_miton_podcast_request_still_uses_episode_resolver(self):
        action = route("Play the latest Miton podcast on the TV")
        self.assertEqual(action.action, "youtube.play_latest")
        self.assertEqual(action.target, "tv")

    def test_google_miton_routes_to_typed_tv_search(self):
        action = route("Google Miton", default_open_target="tv")
        self.assertEqual(action.action, "web.search")
        self.assertEqual(action.target, "tv")
        self.assertEqual(
            action.arguments,
            {"provider": "google", "query": "Miton"},
        )

    def test_google_query_strips_explicit_laptop_target(self):
        action = route("Google Miton on the laptop", default_open_target="tv")
        self.assertEqual(action.target, "notebook")
        self.assertEqual(action.arguments["query"], "Miton")

    def test_factual_question_routes_to_generic_web_search(self):
        action = route(
            "What's the weight of an elephant?",
            default_open_target="tv",
        )
        self.assertEqual(action.action, "web.search")
        self.assertEqual(action.target, "tv")
        self.assertEqual(
            action.arguments,
            {
                "provider": "google",
                "query": "What's the weight of an elephant",
            },
        )

    def test_varied_information_requests_use_the_same_search_capability(self):
        for transcript in (
            "How many species of elephants are there?",
            "Who founded Miton?",
            "Can you tell me how tall the Eiffel Tower is?",
            "Look up the population of Prague",
            "Search for the current time in Tokyo",
        ):
            with self.subTest(transcript=transcript):
                action = route(transcript, default_open_target="tv")
                self.assertEqual(action.action, "web.search")
                self.assertEqual(action.target, "tv")
                self.assertEqual(action.arguments["provider"], "google")

    def test_information_question_can_name_the_notebook_as_target(self):
        action = route(
            "How tall is an elephant on the laptop?",
            default_open_target="tv",
        )
        self.assertEqual(action.target, "notebook")
        self.assertEqual(action.arguments["query"], "How tall is an elephant")

    def test_tv_as_a_subject_is_not_removed_from_information_query(self):
        action = route(
            "What is OLED TV?",
            default_open_target="notebook",
        )
        self.assertEqual(action.target, "notebook")
        self.assertEqual(action.arguments["query"], "What is OLED TV")

    def test_active_google_session_can_open_first_page_without_activation(self):
        action = route(
            "Open the first page",
            active_target="tv",
            require_activation=True,
        )
        self.assertEqual(action.action, "web.open_first_result")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {})

    def test_first_page_requires_an_active_tv_session(self):
        with self.assertRaises(NoRoute):
            route("Open the first page")

    def test_czech_route(self):
        action = route("Otevři YouTube na počítači")
        self.assertEqual(action.arguments, {"site": "youtube"})

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(NoRoute):
            route("Delete my documents")

    def test_latest_cinkatko_routes_to_typed_youtube_action(self):
        action = route("Play the latest Cinkatko podcast on the laptop")
        self.assertEqual(action.action, "youtube.play_latest")
        self.assertEqual(action.target, "notebook")
        self.assertEqual(
            action.arguments,
            {"channel": "cinkatko", "selector": "latest_episode"},
        )

    def test_czech_latest_cinkatko_route(self):
        action = route("Pusť poslední díl Cinkátka na YouTube")
        self.assertEqual(action.action, "youtube.play_latest")

    def test_latest_cinkatko_routes_to_local_tv(self):
        action = route("Play the latest Cinkatko podcast on the TV")
        self.assertEqual(action.action, "youtube.play_latest")
        self.assertEqual(action.target, "tv")

    def test_latest_cinkatko_follow_up_inherits_active_youtube_target(self):
        action = route(
            "Play the latest episode of Cinkatko podcast",
            active_target="tv",
            active_surface="youtube",
            default_open_target="notebook",
        )

        self.assertEqual(action.action, "youtube.play_latest")
        self.assertEqual(action.target, "tv")
        self.assertEqual(
            action.arguments,
            {"channel": "cinkatko", "selector": "latest_episode"},
        )

    def test_active_youtube_accepts_a_generic_play_follow_up(self):
        action = route(
            "Play lo-fi hip hop",
            active_target="notebook",
            active_surface="youtube",
        )

        self.assertEqual(action.action, "youtube.search_play")
        self.assertEqual(action.target, "notebook")
        self.assertEqual(action.arguments, {"query": "lo-fi hip hop"})

        czech_action = route(
            "Pusť relaxační hudbu",
            active_target="notebook",
            active_surface="youtube",
        )
        self.assertEqual(czech_action.action, "youtube.search_play")
        self.assertEqual(czech_action.arguments, {"query": "relaxační hudbu"})

        czech_tv_action = route(
            "Pusť relaxační hudbu na televizi",
            active_target="notebook",
            active_surface="youtube",
        )
        self.assertEqual(czech_tv_action.target, "tv")
        self.assertEqual(czech_tv_action.arguments, {"query": "relaxační hudbu"})

    def test_generic_youtube_follow_up_explicit_target_overrides_context_target(self):
        action = route(
            "Play lo-fi hip hop on the TV",
            active_target="notebook",
            active_surface="youtube",
        )

        self.assertEqual(action.action, "youtube.search_play")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"query": "lo-fi hip hop"})

    def test_generic_play_is_rejected_outside_a_youtube_context(self):
        for active_surface in (None, "miton", "google"):
            with self.subTest(active_surface=active_surface):
                with self.assertRaises(NoRoute):
                    route(
                        "Play lo-fi hip hop",
                        active_target="notebook",
                        active_surface=active_surface,
                    )

    def test_start_tv_app_remains_an_app_outside_youtube_context(self):
        action = route("Start Plex", active_surface="miton")

        self.assertEqual(action.action, "app.open")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"app": "plex"})

    def test_vague_generic_play_is_rejected_even_on_youtube(self):
        for transcript in ("Play something", "Play anything", "Play a video"):
            with self.subTest(transcript=transcript):
                with self.assertRaises(NoRoute):
                    route(
                        transcript,
                        active_target="notebook",
                        active_surface="youtube",
                    )

    def test_activated_youtube_follow_up_works_in_hands_free_mode(self):
        action = route(
            "Room Agent, play an elephant documentary",
            active_target="tv",
            active_surface="youtube",
            require_activation=True,
            default_open_target="tv",
        )

        self.assertEqual(action.action, "youtube.search_play")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"query": "an elephant documentary"})

    def test_named_podcasts_route_to_generic_youtube_discovery(self):
        cases = (
            (
                "Play the latest Lex Fridman podcast on the TV",
                "Lex Fridman",
                "tv",
            ),
            ("Play latest Huberman Lab episode", "Huberman Lab", "tv"),
            ("Play the newest episode of Hard Fork on TV", "Hard Fork", "tv"),
            (
                "Put on Platformer's latest podcast on the television",
                "Platformer",
                "tv",
            ),
            (
                "Show me the most recent Acquired podcast on YouTube",
                "Acquired",
                "tv",
            ),
            (
                "Please play the latest Search Engine podcast on the laptop",
                "Search Engine",
                "notebook",
            ),
        )
        for transcript, query, target in cases:
            with self.subTest(transcript=transcript):
                action = route(transcript, default_open_target="tv")
                self.assertEqual(action.action, "youtube.search_latest")
                self.assertEqual(action.target, target)
                self.assertEqual(
                    action.arguments,
                    {"query": query, "selector": "latest_episode"},
                )

    def test_latest_podcast_requires_a_name(self):
        with self.assertRaises(NoRoute):
            route("Play the latest podcast on the TV")

    def test_conflicting_devices_are_rejected(self):
        with self.assertRaises(NoRoute):
            route("Play Cinkatko on the TV and laptop")

    def test_active_tv_session_can_pause_with_bare_command(self):
        action = route(
            "pause",
            active_target="tv",
            require_activation=True,
        )
        self.assertEqual(action.action, "media.pause")
        self.assertEqual(action.target, "tv")

    def test_active_tv_session_can_pause_the_video_without_activation(self):
        for transcript in (
            "Pause the video",
            "Pause the video please",
            "Please pause the video",
            "Pause playback",
        ):
            with self.subTest(transcript=transcript):
                action = route(
                    transcript,
                    active_target="tv",
                    require_activation=True,
                )
                self.assertEqual(action.action, "media.pause")
                self.assertEqual(action.target, "tv")

    def test_active_tv_session_can_continue_with_bare_command(self):
        action = route(
            "continue",
            active_target="tv",
            require_activation=True,
        )
        self.assertEqual(action.action, "media.resume")

    def test_active_tv_session_can_continue_the_video_without_activation(self):
        for transcript in (
            "Continue the video",
            "Resume the video",
            "Please continue the video",
            "Resume playback",
        ):
            with self.subTest(transcript=transcript):
                action = route(
                    transcript,
                    active_target="tv",
                    require_activation=True,
                )
                self.assertEqual(action.action, "media.resume")
                self.assertEqual(action.target, "tv")

    def test_room_agent_can_close_tv_kiosk(self):
        action = route(
            "close tab",
            active_target="tv",
            require_activation=True,
        )
        self.assertEqual(action.action, "media.close")
        self.assertEqual(action.target, "tv")

    def test_quit_can_close_active_tv_kiosk(self):
        action = route("quit", active_target="tv", require_activation=True)
        self.assertEqual(action.action, "media.close")

    def test_quit_inside_a_sentence_is_not_a_close_command(self):
        with self.assertRaises(NoActivation):
            route(
                "I might quit this job next year",
                active_target="tv",
                require_activation=True,
            )

    def test_close_requires_activation_even_without_tracked_session(self):
        with self.assertRaises(NoActivation):
            route("close the tab")

    def test_tv_audio_without_activation_is_ignored(self):
        with self.assertRaises(NoActivation):
            route(
                "pause for a moment",
                active_target="tv",
                require_activation=True,
            )

    def test_negated_cinkatko_command_is_rejected(self):
        with self.assertRaises(NoRoute):
            route("Do not play Cinkatko")

    def test_generic_tv_app_phrase_becomes_typed_app_action(self):
        action = route("Open Spotify on the TV", default_open_target="tv")

        self.assertEqual(action.action, "app.open")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"app": "spotify"})

    def test_tv_remote_phrases_become_bounded_typed_actions(self):
        cases = (
            ("Turn the TV off", "power_off", 1),
            ("Increase the volume by 20", "volume_up", 20),
            ("Move left", "navigate_left", 1),
            ("Vypni televizi", "power_off", 1),
            ("Zesil hlasitost o 5", "volume_up", 5),
        )
        for transcript, command, count in cases:
            with self.subTest(transcript=transcript):
                action = route(transcript)
                self.assertEqual(action.action, "tv.remote")
                self.assertEqual(action.target, "tv")
                self.assertEqual(
                    action.arguments,
                    {"command": command, "count": count},
                )

    def test_tv_remote_repetition_is_bounded(self):
        with self.assertRaises(NoRoute):
            route("Increase the volume by 21")


class LocalLlmRouterTests(unittest.TestCase):
    @staticmethod
    def response(action, target, query):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action":' + json.dumps(action)
                            + ',"target":' + json.dumps(target)
                            + ',"query":' + json.dumps(query)
                            + "}"
                        )
                    }
                }
            ]
        }

    def test_arbitrary_podcast_maps_to_typed_youtube_search(self):
        calls = []

        def requester(endpoint, payload, timeout):
            calls.append((endpoint, payload, timeout))
            return self.response("play_latest", "tv", "Lex Fridman")

        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            timeout=4,
            requester=requester,
        )
        action = router.route("Put the newest Lex Fridman conversation on the TV")

        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.target, "tv")
        self.assertEqual(
            action.arguments,
            {"query": "Lex Fridman", "selector": "latest_episode"},
        )
        self.assertEqual(calls[0][0], "http://127.0.0.1:8767/v1/chat/completions")
        self.assertEqual(calls[0][2], 4)

    def test_cinkatko_model_result_uses_exact_channel_resolver(self):
        router = LocalLlmRouter(
            "http://localhost:8767",
            requester=lambda *_args: self.response("play_latest", "tv", "Cinkatko"),
        )

        action = router.route("Pust nejnovejsi Cinkatko na televizi")

        self.assertEqual(action.action, "youtube.play_latest")
        self.assertEqual(
            action.arguments,
            {"channel": "cinkatko", "selector": "latest_episode"},
        )

    def test_model_open_result_can_target_any_discovered_native_tv_app(self):
        router = LocalLlmRouter(
            "http://localhost:8767",
            requester=lambda *_args: self.response("open", "tv", "Plex"),
        )

        action = router.route("Could you bring up Plex on the television?")

        self.assertEqual(action.action, "app.open")
        self.assertEqual(action.arguments, {"app": "plex"})

    def test_media_control_uses_active_target_instead_of_model_target(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("close", "notebook", ""),
        )

        action = router.route("That is enough, close the tab", active_target="tv")

        self.assertEqual(action.action, "media.close")
        self.assertEqual(action.target, "tv")

    def test_media_control_without_active_tv_is_rejected(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("pause", "tv", ""),
        )
        with self.assertRaises(LocalLlmError):
            router.route("Pause it")

    def test_deterministic_match_does_not_call_model(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.fail("The model should not be called."),
        )

        action = hybrid_route("Open YouTube on the laptop", llm_router=router)

        self.assertEqual(action.action, "web.open")

    def test_common_named_podcast_phrase_does_not_call_model(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.fail("The model should not be called."),
        )

        action = hybrid_route(
            "Play the latest Lex Fridman podcast on the TV",
            llm_router=router,
        )

        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.arguments["query"], "Lex Fridman")

    def test_negated_command_does_not_fall_through_to_model(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.fail("The model should not be called."),
        )
        with self.assertRaises(NoRoute):
            hybrid_route("Do not play Lex Fridman", llm_router=router)

    def test_generic_play_outside_youtube_does_not_fall_through_to_model(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.fail("The model should not be called."),
        )

        for transcript in (
            "Play an elephant documentary",
            "Could you play an elephant documentary?",
        ):
            with self.subTest(transcript=transcript):
                with self.assertRaises(NoRoute):
                    hybrid_route(
                        transcript,
                        active_target="notebook",
                        active_surface="miton",
                        llm_router=router,
                    )

    def test_active_session_without_activation_does_not_call_model(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.fail("The model should not be called."),
        )
        with self.assertRaises(NoActivation):
            hybrid_route(
                "Put Lex Fridman on the TV",
                active_target="tv",
                require_activation=True,
                llm_router=router,
            )

    def test_unknown_command_uses_model_fallback(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("play_latest", "tv", "Huberman Lab"),
        )

        action = hybrid_route(
            "Could you put Huberman Lab on the big screen?",
            llm_router=router,
        )

        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.arguments["query"], "Huberman Lab")

    def test_non_loopback_model_url_is_rejected(self):
        with self.assertRaises(ValueError):
            LocalLlmRouter("http://192.168.1.5:8767")

    def test_model_can_open_allowlisted_miton_site_on_tv(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("open", "tv", "Miton.cz"),
        )

        action = router.route("Show me the Miton website on the TV")

        self.assertEqual(action.action, "web.open")
        self.assertEqual(action.target, "tv")
        self.assertEqual(action.arguments, {"site": "miton"})

    def test_model_can_create_typed_google_search(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("search", "tv", "Miton"),
        )

        action = router.route("Look up Miton on Google")

        self.assertEqual(action.action, "web.search")
        self.assertEqual(
            action.arguments,
            {"provider": "google", "query": "Miton"},
        )

    def test_model_remote_result_becomes_typed_bounded_action(self):
        router = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("remote", "tv", "volume_down 7"),
        )

        action = router.route("Could you lower the television a little?")

        self.assertEqual(action.action, "tv.remote")
        self.assertEqual(
            action.arguments,
            {"command": "volume_down", "count": 7},
        )


class OpenRouterLlmRouterTests(unittest.TestCase):
    @staticmethod
    def response(action, target, query):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action": action, "target": target, "query": query}
                        )
                    }
                }
            ]
        }

    def test_openrouter_uses_authenticated_strict_structured_output(self):
        calls = []

        def requester(endpoint, payload, timeout, api_key):
            calls.append((endpoint, payload, timeout, api_key))
            return self.response("play_latest", "tv", "Acquired")

        router = OpenRouterLlmRouter(
            "sk-or-test-key",
            model="deepseek/deepseek-v4-flash",
            timeout=3.5,
            requester=requester,
        )
        action = router.route("Find the newest Acquired conversation for the TV")

        endpoint, payload, timeout, api_key = calls[0]
        schema = payload["response_format"]["json_schema"]
        self.assertEqual(endpoint, OPENROUTER_ENDPOINT)
        self.assertEqual(api_key, "sk-or-test-key")
        self.assertEqual(timeout, 3.5)
        self.assertEqual(payload["model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(payload["max_tokens"], 160)
        self.assertEqual(
            payload["reasoning"],
            {"effort": "none", "exclude": True},
        )
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.arguments["query"], "Acquired")
        self.assertEqual(router.last_backend, "openrouter")

    def test_openrouter_http_request_uses_bearer_authentication(self):
        captured = {}

        class RequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - HTTP handler API
                length = int(self.headers["Content-Length"])
                captured["authorization"] = self.headers.get("Authorization")
                captured["payload"] = json.loads(self.rfile.read(length))
                response = json.dumps(
                    OpenRouterLlmRouterTests.response("search", "tv", "Miton")
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request_openrouter_json(
                f"http://127.0.0.1:{server.server_port}/chat/completions",
                {"model": "test", "messages": []},
                2,
                "secret-test-key",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(captured["authorization"], "Bearer secret-test-key")
        self.assertEqual(captured["payload"]["model"], "test")
        self.assertIn("choices", response)

    def test_openrouter_failure_falls_back_to_local_qwen(self):
        def unavailable(*_args):
            raise OpenRouterLlmError("offline")

        cloud = OpenRouterLlmRouter(
            "sk-or-test-key",
            requester=unavailable,
        )
        local = LocalLlmRouter(
            "http://127.0.0.1:8767",
            requester=lambda *_args: self.response("search", "tv", "Miton AI"),
        )
        router = FallbackLlmRouter((cloud, local))

        action = hybrid_route(
            "Could you investigate Miton's work in AI on the big screen?",
            llm_router=router,
        )

        self.assertEqual(action.action, "web.search")
        self.assertEqual(action.arguments["query"], "Miton AI")
        self.assertEqual(router.last_backend, "local")

    def test_openrouter_rejects_invalid_secret_and_model(self):
        with self.assertRaises(ValueError):
            OpenRouterLlmRouter("key with whitespace")
        with self.assertRaises(ValueError):
            OpenRouterLlmRouter("test-key", model="https://example.com/model")

    def test_pi_prefers_openrouter_and_keeps_local_fallback(self):
        router = build_semantic_router(
            openrouter_api_key="sk-or-test-key",
            local_url="http://127.0.0.1:8767",
        )

        self.assertIsInstance(router, FallbackLlmRouter)
        self.assertEqual(
            [candidate.backend_name for candidate in router.routers],
            ["openrouter", "local"],
        )


class HermesLlmRouterTests(unittest.TestCase):
    @staticmethod
    def response(action, target, query, *, fenced=False):
        content = json.dumps({"action": action, "target": target, "query": query})
        if fenced:
            content = f"```json\n{content}\n```"
        return {"choices": [{"message": {"content": content}}]}

    def test_hermes_is_first_with_cloud_and_local_fallbacks(self):
        router = build_semantic_router(
            hermes_url="http://127.0.0.1:8642",
            hermes_api_key="local-sidecar-secret",
            openrouter_api_key="sk-or-test-key",
            local_url="http://127.0.0.1:8767",
        )

        self.assertIsInstance(router, FallbackLlmRouter)
        self.assertEqual(
            [candidate.backend_name for candidate in router.routers],
            ["hermes", "openrouter", "local"],
        )

    def test_hermes_request_is_authenticated_and_bounded(self):
        calls = []

        def requester(endpoint, payload, timeout, api_key):
            calls.append((endpoint, payload, timeout, api_key))
            return self.response("play_latest", "tv", "Y Combinator")

        router = HermesLlmRouter(
            "http://localhost:8642",
            "local-sidecar-secret",
            timeout=19,
            requester=requester,
        )
        action = router.route("Put the latest YC podcast on the big screen")

        endpoint, payload, timeout, api_key = calls[0]
        self.assertEqual(endpoint, "http://localhost:8642/v1/chat/completions")
        self.assertEqual(timeout, 19)
        self.assertEqual(api_key, "local-sidecar-secret")
        self.assertEqual(payload["model"], "hermes-agent")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["temperature"], 0)
        self.assertIn("trusted room executor", payload["messages"][0]["content"])
        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.arguments["query"], "Y Combinator")
        self.assertEqual(router.last_backend, "hermes")

    def test_hermes_http_request_uses_bearer_authentication(self):
        captured = {}

        class RequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - HTTP handler API
                length = int(self.headers["Content-Length"])
                captured["authorization"] = self.headers.get("Authorization")
                captured["payload"] = json.loads(self.rfile.read(length))
                response = json.dumps(
                    HermesLlmRouterTests.response("search", "tv", "elephant weight")
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = request_hermes_json(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                {"model": "hermes-agent", "messages": []},
                2,
                "local-sidecar-secret",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(captured["authorization"], "Bearer local-sidecar-secret")
        self.assertEqual(captured["payload"]["model"], "hermes-agent")
        self.assertIn("choices", response)

    def test_hermes_accepts_fenced_json_but_rejects_extra_fields(self):
        fenced = HermesLlmRouter(
            "http://127.0.0.1:8642",
            "local-sidecar-secret",
            requester=lambda *_args: self.response("search", "tv", "Miton", fenced=True),
        )
        self.assertEqual(fenced.route("Google Miton").action, "web.search")

        invalid = HermesLlmRouter(
            "http://127.0.0.1:8642",
            "local-sidecar-secret",
            requester=lambda *_args: {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "search",
                                    "target": "tv",
                                    "query": "Miton",
                                    "shell": "anything",
                                }
                            )
                        }
                    }
                ]
            },
        )
        with self.assertRaises(HermesLlmError):
            invalid.route("Google Miton")

    def test_hermes_requires_loopback_auth_pair(self):
        with self.assertRaises(ValueError):
            HermesLlmRouter("http://192.168.1.20:8642", "secret")
        with self.assertRaises(ValueError):
            HermesLlmRouter("http://127.0.0.1:8642", "key with whitespace")
        with self.assertRaises(ValueError):
            build_semantic_router(hermes_url="http://127.0.0.1:8642")

    def test_hermes_failure_falls_back_to_direct_openrouter(self):
        hermes = HermesLlmRouter(
            "http://127.0.0.1:8642",
            "local-sidecar-secret",
            requester=lambda *_args: (_ for _ in ()).throw(
                HermesLlmError("sidecar offline")
            ),
        )
        cloud = OpenRouterLlmRouter(
            "sk-or-test-key",
            requester=lambda *_args: self.response("play_latest", "tv", "Acquired"),
        )
        router = FallbackLlmRouter((hermes, cloud))

        action = hybrid_route(
            "Could you find Acquired's most recent release for the screen?",
            llm_router=router,
        )

        self.assertEqual(action.action, "youtube.search_latest")
        self.assertEqual(action.arguments["query"], "Acquired")
        self.assertEqual(router.last_backend, "openrouter")


class CompanionTests(unittest.TestCase):
    def test_allowlisted_site_opens(self):
        calls = []

        def opener(url, **kwargs):
            calls.append((url, kwargs))
            return True

        action = ActionRequest.create("web.open", "notebook", {"site": "youtube"})
        self.assertEqual(
            execute_action(action, opener),
            {"opened": "https://www.youtube.com/"},
        )
        self.assertEqual(calls[0][0], "https://www.youtube.com/")

    def test_allowlisted_miton_site_opens(self):
        calls = []

        action = ActionRequest.create("web.open", "notebook", {"site": "miton"})
        result = execute_action(
            action,
            lambda url, **kwargs: calls.append(url) is None,
        )

        self.assertEqual(result, {"opened": "https://www.miton.cz/"})
        self.assertEqual(calls, ["https://www.miton.cz/"])

    def test_typed_google_search_opens_encoded_query(self):
        calls = []
        action = ActionRequest.create(
            "web.search",
            "notebook",
            {"provider": "google", "query": "Miton AI"},
        )

        result = execute_action(
            action,
            lambda url, **kwargs: calls.append(url) is None,
        )

        self.assertEqual(calls, ["https://www.google.com/search?q=Miton+AI"])
        self.assertEqual(result["query"], "Miton AI")

    def test_latest_cinkatko_opens_only_the_resolved_video(self):
        calls = []
        media = MediaItem(
            video_id="episode1234",
            title="Newest full episode",
            published_at="2026-08-03T10:00:00Z",
            duration_seconds=2_700,
        )
        action = ActionRequest.create(
            "youtube.play_latest",
            "notebook",
            {"channel": "cinkatko", "selector": "latest_episode"},
        )

        result = execute_action(
            action,
            lambda url, **kwargs: calls.append(url) is None,
            latest_resolver=lambda: media,
        )

        self.assertEqual(calls, [media.url])
        self.assertEqual(result["video_id"], "episode1234")
        self.assertEqual(result["title"], "Newest full episode")

    def test_latest_youtube_rejects_unapproved_channel(self):
        action = ActionRequest.create(
            "youtube.play_latest",
            "notebook",
            {"channel": "someone_else", "selector": "latest_episode"},
        )
        with self.assertRaises(ActionError):
            execute_action(action, lambda *args, **kwargs: True, latest_resolver=lambda: None)

    def test_search_latest_opens_only_resolved_video(self):
        opened = []
        queries = []
        media = MediaItem(
            video_id="episode1234",
            title="Newest Lex Fridman episode",
            published_at="2026-08-04T10:00:00Z",
            duration_seconds=7_200,
        )
        action = ActionRequest.create(
            "youtube.search_latest",
            "notebook",
            {"query": "Lex Fridman", "selector": "latest_episode"},
        )

        result = execute_action(
            action,
            lambda url, **kwargs: opened.append(url) is None,
            search_resolver=lambda query: queries.append(query) or media,
        )

        self.assertEqual(queries, ["Lex Fridman"])
        self.assertEqual(opened, [media.url])
        self.assertEqual(result["video_id"], "episode1234")

    def test_arbitrary_url_is_rejected(self):
        action = ActionRequest.create("web.open", "notebook", {"site": "example"})
        with self.assertRaises(ActionError):
            execute_action(action, lambda *args, **kwargs: True)

    def test_arbitrary_action_is_rejected(self):
        action = ActionRequest.create("shell.run", "notebook", {"command": "anything"})
        with self.assertRaises(ActionError):
            execute_action(action, lambda *args, **kwargs: True)


class FakeWindowsProcess:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


class WindowsBrowserTests(unittest.TestCase):
    def make_controller(self, root, calls, media_keys=None):
        browser = root / "msedge.exe"
        browser.write_bytes(b"")
        extension = root / "extension"
        extension.mkdir()
        (extension / "manifest.json").write_text("{}", encoding="utf-8")

        def launcher(command, **kwargs):
            calls.append(("launch", command, kwargs))
            return FakeWindowsProcess(4_000 + len(calls))

        def runner(command, **kwargs):
            calls.append(("run", command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        return WindowsBrowserController(
            browser=browser,
            profile_dir=root / "profile",
            extension_dir=extension,
            launcher=launcher,
            runner=runner,
            media_key_sender=(
                (lambda: media_keys.append("toggle"))
                if media_keys is not None
                else None
            ),
            window_activator=lambda pid: calls.append(("activate", pid)) or True,
            sleeper=lambda _seconds: None,
        )

    def test_search_and_first_result_use_only_the_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            controller = self.make_controller(Path(directory), calls)

            controller.search_google("Miton AI")
            controller.open_first_google_result()

        launches = [call[1] for call in calls if call[0] == "launch"]
        kills = [call[1] for call in calls if call[0] == "run"]
        activations = [call[1] for call in calls if call[0] == "activate"]
        self.assertEqual(len(launches), 2)
        self.assertEqual(len(activations), 2)
        self.assertIn("--new-window", launches[0])
        self.assertTrue(any(value.startswith("https://www.google.com/search?") for value in launches[0]))
        self.assertTrue(any(value.endswith("#room-agent-open-first") for value in launches[1]))
        self.assertEqual(kills[0][0:2], ["taskkill", "/PID"])

    def test_pause_and_resume_are_idempotent_media_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            media_keys = []
            controller = self.make_controller(Path(directory), calls, media_keys)
            controller.play_youtube("abc123XYZ_-"[:11])

            controller.pause()
            controller.pause()
            controller.resume()
            controller.resume()

        self.assertEqual(len(media_keys), 2)

    def test_personal_profile_tracks_and_closes_only_its_new_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "brave.exe"
            browser.write_bytes(b"")
            calls = []
            snapshots = iter(({101}, {101, 202}))
            activated = []
            closed = []

            def launcher(command, **kwargs):
                calls.append((command, kwargs))
                return FakeWindowsProcess(4_200)

            controller = WindowsBrowserController(
                browser=browser,
                personal_profile=True,
                launcher=launcher,
                window_snapshotter=lambda: set(next(snapshots)),
                window_handle_activator=lambda window: activated.append(window) or True,
                tracked_window_closer=lambda window: closed.append(window) or True,
                sleeper=lambda _seconds: None,
            )

            opened = controller.open_site("youtube")
            result = controller.close()

        command = calls[0][0]
        self.assertEqual(command, [str(browser), "--new-window", "https://www.youtube.com/"])
        self.assertFalse(any(value.startswith("--user-data-dir=") for value in command))
        self.assertEqual(opened["player"], "windows-personal-browser")
        self.assertEqual(activated, [202])
        self.assertEqual(closed, [202])
        self.assertEqual(result["closed_window"], "202")


class FakeWindowsBrowser:
    def __init__(self):
        self.calls = []

    def open_site(self, site):
        self.calls.append(("open", site))
        return {"opened": f"https://{site}.example/"}

    def pause(self):
        self.calls.append(("pause",))
        return {"media_status": "Paused"}

    def play_youtube(self, video_id):
        self.calls.append(("play_youtube", video_id))
        return {
            "opened": f"https://www.youtube.com/watch?v={video_id}&autoplay=1",
            "player": "windows-personal-browser",
        }


class RecordingTvConnector(TvConnectorClient):
    """Capture validated plans without requiring connector hardware."""

    def __init__(self, *, profile=DEFAULT_CONNECTOR_PROFILE):
        super().__init__(bytes.fromhex("3d" * 32), profile=profile)
        self.executions = []

    def health(self):
        return {"status": "ok", "profile": self.profile}

    def _execute(self, purpose, steps):
        captured = tuple(steps)
        self.executions.append((purpose, captured))
        return {
            "connector_purpose": purpose,
            "connector_profile": self.profile,
        }


class TvConnectorTests(unittest.TestCase):
    @staticmethod
    def _health_response(
        *,
        version: int,
        response_auth: str | None = None,
        boot_id: str | None = None,
    ):
        payload = {
            "status": "ok",
            "version": version,
            "profile": DEFAULT_CONNECTOR_PROFILE,
        }
        if response_auth is not None:
            payload["response_auth"] = response_auth
        if boot_id is not None:
            payload["boot_id"] = boot_id
        entered = mock.MagicMock()
        entered.read.return_value = json.dumps(payload).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value = entered
        return response

    @staticmethod
    def _execution_response(
        secret: bytes,
        request: urllib.request.Request,
        *,
        overrides: dict | None = None,
        signed: bool = True,
    ):
        plan = ConnectorPlan.from_bytes(request.data or b"")
        payload = {
            "version": CONNECTOR_VERSION,
            "status": "executed",
            "request_id": plan.request_id,
            "profile": plan.profile,
            "purpose": plan.purpose,
            "steps_executed": len(plan.steps),
            "transport": "simulated",
        }
        payload.update(overrides or {})
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        entered = mock.MagicMock()
        entered.read.return_value = body
        entered.headers = (
            {"X-Room-Agent-Response-Signature": sign_response(secret, body)}
            if signed
            else {}
        )
        response = mock.MagicMock()
        response.__enter__.return_value = entered
        return response

    def _compatible_opener(
        self,
        secret: bytes,
        *,
        overrides: dict | None = None,
        signed: bool = True,
    ):
        def open_request(request, **_kwargs):
            if request.get_method() == "GET":
                return self._health_response(
                    version=CONNECTOR_VERSION,
                    response_auth="hmac-sha256-v1",
                    boot_id=TEST_CONNECTOR_BOOT_ID,
                )
            return self._execution_response(
                secret,
                request,
                overrides=overrides,
                signed=signed,
            )

        return mock.Mock(side_effect=open_request)

    def test_connector_discovery_uses_packet_source_address(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        def announce():
            time.sleep(0.05)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(
                    json.dumps(
                        {
                            "magic": "room-agent-tv-connector",
                            "version": 1,
                            "name": "atoms3u-tv-01",
                            "profile": "generic-usb-hid-v1",
                            "ip": "203.0.113.10",
                            "port": 80,
                            "wifi_rssi": -48,
                        }
                    ).encode("utf-8"),
                    ("127.0.0.1", port),
                )
            finally:
                sender.close()

        thread = threading.Thread(target=announce, daemon=True)
        thread.start()
        connectors = discover_connectors(
            0.3,
            port=port,
            bind_host="127.0.0.1",
        )
        thread.join(timeout=1)

        self.assertEqual(len(connectors), 1)
        self.assertEqual(connectors[0]["ip"], "127.0.0.1")
        self.assertEqual(connectors[0]["url"], "http://127.0.0.1:80")

    def test_connector_discovery_rejects_non_finite_timeout(self):
        with self.assertRaisesRegex(ProtocolError, "between 0 and 60"):
            discover_connectors(float("nan"))

    def test_firmware_network_provisioning_preserves_other_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "compile-test"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "compile-only"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "ab" * 32 + '"\n',
                description="test firmware settings",
            )

            provision_firmware_network(
                'Office "Demo"', r"safe\\password", secrets_file
            )

            updated = secrets_file.read_text(encoding="utf-8")
            self.assertIn(r'ROOM_AGENT_WIFI_SSID "Office \"Demo\""', updated)
            self.assertIn(r'ROOM_AGENT_WIFI_PASSWORD "safe\\\\password"', updated)
            self.assertIn('"' + "ab" * 32 + '"', updated)

            provision_firmware_network(
                'Second "Office"', r"second\\password", secrets_file
            )
            updated_again = secrets_file.read_text(encoding="utf-8")
            self.assertIn(r'ROOM_AGENT_WIFI_SSID "Second \"Office\""', updated_again)
            self.assertIn(
                r'ROOM_AGENT_WIFI_PASSWORD "second\\\\password"',
                updated_again,
            )

    def test_firmware_provisioning_rejects_non_utf8_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                "ÿþ",
                encoding="latin-1",
                description="test malformed firmware settings",
            )

            with self.assertRaisesRegex(ProtocolError, "firmware secrets"):
                provision_firmware_network("Office", "safe-password", secrets_file)

    def test_firmware_secret_updates_restrict_replacement_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            secrets_file = root / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "compile-test"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "compile-only"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                description="test firmware settings",
            )

            provision_firmware_network("Office", "safe-password", secrets_file)
            sync_firmware_key(key_file, secrets_file)
            configure_firmware_profile(LG_TV_IR_PROFILE, secrets_file)

            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(secrets_file.stat().st_mode), 0o600)
            self.assertFalse(secrets_file.with_suffix(".h.tmp").exists())

    def test_firmware_key_sync_replaces_only_the_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            secrets_file = root / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "office"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX \\\n'
                f'  "{"0" * 64}"\n',
                description="test firmware settings",
            )

            sync_firmware_key(key_file, secrets_file)

            updated = secrets_file.read_text(encoding="utf-8")
            self.assertIn('ROOM_AGENT_WIFI_SSID "office"', updated)
            self.assertIn('"' + "ab" * 32 + '"', updated)
            self.assertNotIn('"' + "0" * 64 + '"', updated)

    def test_firmware_profile_selection_preserves_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "RoomAgent-Link"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "keep-private"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "ab" * 32 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                description="test firmware settings",
            )

            configure_firmware_profile(SONY_BRAVIA_IR_PROFILE, secrets_file)

            updated = secrets_file.read_text(encoding="utf-8")
            self.assertIn(
                f'ROOM_AGENT_CONNECTOR_PROFILE "{SONY_BRAVIA_IR_PROFILE}"',
                updated,
            )
            self.assertIn('ROOM_AGENT_WIFI_PASSWORD "keep-private"', updated)
            self.assertIn('"' + "ab" * 32 + '"', updated)
            self.assertNotIn(
                f'ROOM_AGENT_CONNECTOR_PROFILE "{DEFAULT_CONNECTOR_PROFILE}"',
                updated,
            )

    def test_firmware_profile_selection_requires_exactly_one_header_value(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                "// profile is missing\n",
                description="test firmware settings",
            )

            with self.assertRaisesRegex(ProtocolError, "exactly one"):
                configure_firmware_profile(SONY_BRAVIA_IR_PROFILE, secrets_file)

    def test_lg_firmware_profile_selection_preserves_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "RoomAgent-Link"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "keep-private"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "cd" * 32 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "sony-bravia-ir-v1"\n',
                description="test firmware settings",
            )

            configure_firmware_profile(LG_TV_IR_PROFILE, secrets_file)

            updated = secrets_file.read_text(encoding="utf-8")
            self.assertIn(
                f'ROOM_AGENT_CONNECTOR_PROFILE "{LG_TV_IR_PROFILE}"',
                updated,
            )
            self.assertIn('ROOM_AGENT_WIFI_PASSWORD "keep-private"', updated)
            self.assertIn('"' + "cd" * 32 + '"', updated)
            self.assertNotIn(
                f'ROOM_AGENT_CONNECTOR_PROFILE "{SONY_BRAVIA_IR_PROFILE}"',
                updated,
            )

    def test_lg_hybrid_firmware_profile_selection_preserves_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets_file = Path(directory) / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "RoomAgent-Link"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "keep-private"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "ce" * 32 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "lg-tv-ir-v1"\n',
                description="test firmware settings",
            )

            configure_firmware_profile(LG_WEBOS_HYBRID_PROFILE, secrets_file)

            updated = secrets_file.read_text(encoding="utf-8")
            self.assertIn(
                f'ROOM_AGENT_CONNECTOR_PROFILE "{LG_WEBOS_HYBRID_PROFILE}"',
                updated,
            )
            self.assertIn('ROOM_AGENT_WIFI_PASSWORD "keep-private"', updated)
            self.assertIn('"' + "ce" * 32 + '"', updated)

    def test_ble_host_configuration_is_disabled_until_protocol_v2_is_authenticated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            env_file = root / ".env"
            original = (
                "OPENROUTER_API_KEY=keep-this-private\n"
                "TV_CONNECTOR_URL=http://old-connector\n"
            )
            env_file.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ProtocolError, "disabled.*protocol v2"):
                configure_ble_host(
                    env_file,
                    key_file,
                    "atoms3u-tv-01",
                    DEFAULT_CONNECTOR_PROFILE,
                )
            self.assertEqual(env_file.read_text(encoding="utf-8"), original)

    @mock.patch("configure_connector.UsbTvConnectorClient")
    @mock.patch("configure_connector._replace_private_text")
    def test_host_environment_uses_private_atomic_replacement(
        self, replace_private, client_type
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            env_file = root / ".env"
            client_type.return_value.base_url = "http://connector.local"

            configure_wifi_host(
                env_file,
                key_file,
                "http://connector.local",
                DEFAULT_CONNECTOR_PROFILE,
            )

            replace_private.assert_called_once()
            self.assertEqual(replace_private.call_args.args[0], env_file)

    def test_wifi_host_configuration_selects_url_and_preserves_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ac" * 32, description="test connector key")
            env_file = root / ".env"
            env_file.write_text(
                "OPENROUTER_API_KEY=keep-this-private\n"
                "TV_CONNECTOR_BLE_NAME=old-ble-device\n",
                encoding="utf-8",
            )

            configure_wifi_host(
                env_file,
                key_file,
                "http://192.168.137.70:80/",
                "generic-usb-hid-v1",
            )

            updated = env_file.read_text(encoding="utf-8")
            self.assertIn("OPENROUTER_API_KEY=keep-this-private", updated)
            self.assertIn("TV_CONNECTOR_BLE_NAME=\n", updated)
            self.assertIn("TV_CONNECTOR_URL=http://192.168.137.70:80\n", updated)
            self.assertIn(f"TV_CONNECTOR_KEY_FILE={key_file.resolve()}", updated)

    @mock.patch("configure_connector.configure_wifi_host")
    @mock.patch("configure_connector.UsbTvConnectorClient")
    def test_enable_wifi_host_refuses_an_unauthenticated_connector(
        self, client_type, configure_host
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ac" * 32, description="test connector key")
            client_type.return_value.health.return_value = {
                "profile": "generic-usb-hid-v1"
            }
            client_type.return_value.verify.side_effect = TvConnectorError(
                "invalid response signature"
            )
            arguments = [
                "configure_connector.py",
                "enable-wifi-host",
                "--url",
                "http://192.168.137.70",
                "--key-file",
                str(key_file),
                "--env-file",
                str(root / ".env"),
            ]

            with mock.patch("sys.argv", arguments):
                result = configure_connector_main()

            self.assertEqual(result, 2)
            client_type.return_value.verify.assert_called_once_with()
            configure_host.assert_not_called()

    def test_enable_wifi_host_reports_a_malformed_origin_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "connector.key"
            write_private_text(key_file, "ac" * 32, description="test connector key")
            arguments = [
                "configure_connector.py",
                "enable-wifi-host",
                "--url",
                "http://localhost:abc",
                "--key-file",
                str(key_file),
                "--env-file",
                str(root / ".env"),
            ]
            stdout = StringIO()
            stderr = StringIO()

            with (
                mock.patch("sys.argv", arguments),
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                result = configure_connector_main()

            output = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(result, 2)
            self.assertIn("HTTP(S) origin", output)
            self.assertNotIn("Traceback", output)

    def test_connector_plan_round_trips_and_rejects_unbounded_steps(self):
        plan = ConnectorPlan.create(
            "app.open",
            (
                ConnectorStep.key("HOME"),
                ConnectorStep.wait(500),
                ConnectorStep.text("YouTube"),
                ConnectorStep.key("ENTER"),
            ),
            boot_id=TEST_CONNECTOR_BOOT_ID,
        )

        decoded = ConnectorPlan.from_bytes(plan.to_bytes())

        self.assertEqual(decoded, plan)
        with self.assertRaises(TvConnectorError):
            ConnectorPlan.create(
                "invalid.wait",
                (ConnectorStep.wait(5_001),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
            )
        with self.assertRaises(TvConnectorError):
            ConnectorPlan.create(
                "invalid.text",
                (ConnectorStep.text("Příliš brzy"),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
            )

    def test_connector_plan_rejects_wrong_primitive_types_as_domain_errors(self):
        plan = ConnectorPlan.create(
            "remote.mute",
            (ConnectorStep.key("MUTE"),),
            boot_id=TEST_CONNECTOR_BOOT_ID,
        )
        valid = json.loads(plan.to_bytes())
        cases = (
            ("version", 2.0),
            ("request_id", 7),
            ("profile", 7),
            ("purpose", 7),
        )
        for field, value in cases:
            with self.subTest(field=field):
                malformed = dict(valid)
                malformed[field] = value
                with self.assertRaises(TvConnectorError):
                    ConnectorPlan.from_bytes(json.dumps(malformed).encode("ascii"))
        malformed = dict(valid)
        malformed["steps"] = [{"op": 7, "7": "MUTE"}]
        with self.assertRaises(TvConnectorError):
            ConnectorPlan.from_bytes(json.dumps(malformed).encode("ascii"))

    def test_connector_plan_rejects_embedded_nuls_in_every_string_field(self):
        plan = ConnectorPlan.create(
            "remote.mute",
            (ConnectorStep.key("MUTE"),),
            boot_id=TEST_CONNECTOR_BOOT_ID,
        )
        valid = json.loads(plan.to_bytes())
        for field in ("request_id", "boot_id", "profile", "purpose"):
            with self.subTest(field=field):
                malformed = json.loads(json.dumps(valid))
                malformed[field] += "\x00suffix"
                with self.assertRaises(TvConnectorError):
                    ConnectorPlan.from_bytes(json.dumps(malformed).encode("ascii"))

        for field in ("op", "key"):
            with self.subTest(step_field=field):
                malformed = json.loads(json.dumps(valid))
                malformed["steps"][0][field] += "\x00suffix"
                with self.assertRaises(TvConnectorError):
                    ConnectorPlan.from_bytes(json.dumps(malformed).encode("ascii"))

        malformed = json.loads(json.dumps(valid))
        malformed["steps"] = [{"op": "text", "text": "hello\x00world"}]
        with self.assertRaises(TvConnectorError):
            ConnectorPlan.from_bytes(json.dumps(malformed).encode("ascii"))

    def test_remote_key_helper_builds_a_bounded_repeated_plan(self):
        client = RecordingTvConnector(profile=SONY_BRAVIA_IR_PROFILE)

        result = client.press_key("volume_up", count=3)

        self.assertEqual(result["connector_purpose"], "remote.volume_up")
        purpose, steps = client.executions[-1]
        self.assertEqual(purpose, "remote.volume_up")
        self.assertEqual(
            steps,
            (
                ConnectorStep.key("VOLUME_UP"),
                ConnectorStep.key("VOLUME_UP"),
                ConnectorStep.key("VOLUME_UP"),
            ),
        )
        maximum = client.press_key("VOLUME_UP", count=20)
        self.assertEqual(maximum["connector_purpose"], "remote.volume_up")
        self.assertEqual(len(client.executions[-1][1]), 20)
        with self.assertRaisesRegex(TvConnectorError, "between 1 and 20"):
            client.press_key("VOLUME_UP", count=21)
        with self.assertRaisesRegex(TvConnectorError, "not allowlisted"):
            client.press_key("FACTORY_RESET")

    def test_sony_profile_uses_direct_netflix_and_distinct_media_keys(self):
        client = RecordingTvConnector(profile=SONY_BRAVIA_IR_PROFILE)

        opened = client.launch_app("Netflix")
        paused = client.pause()
        resumed = client.resume()

        self.assertEqual(opened["opened"], "Netflix")
        self.assertEqual(paused["media_status"], "Paused")
        self.assertEqual(resumed["media_status"], "Playing")
        self.assertEqual(
            client.executions,
            [
                ("app.open", (ConnectorStep.key("NETFLIX"),)),
                ("media.pause", (ConnectorStep.key("PAUSE"),)),
                ("media.resume", (ConnectorStep.key("PLAY"),)),
            ],
        )

    def test_sony_profile_rejects_unverified_app_and_text_search(self):
        client = RecordingTvConnector(profile=SONY_BRAVIA_IR_PROFILE)

        with self.assertRaisesRegex(TvConnectorError, "no verified key"):
            client.launch_app("YouTube")
        with self.assertRaisesRegex(TvConnectorError, "Text search is unavailable"):
            client.search_google("weight of an elephant")
        self.assertEqual(client.executions, [])

    def test_sony_profile_exposes_discrete_remote_controls(self):
        client = RecordingTvConnector(profile=SONY_BRAVIA_IR_PROFILE)

        client.power_on()
        client.power_off()
        client.input_source()
        client.navigate("left", count=2)
        client.select()

        self.assertEqual(
            [purpose for purpose, _steps in client.executions],
            [
                "remote.power_on",
                "remote.power_off",
                "remote.input",
                "remote.left",
                "remote.enter",
            ],
        )

    def test_lg_profile_accepts_only_its_bounded_remote_key_set(self):
        supported = (
            "POWER",
            "POWER_ON",
            "POWER_OFF",
            "HOME",
            "BACK",
            "UP",
            "DOWN",
            "LEFT",
            "RIGHT",
            "ENTER",
            "PLAY",
            "PAUSE",
            "MUTE",
            "VOLUME_UP",
            "VOLUME_DOWN",
            "INPUT",
        )
        for key in supported:
            plan = ConnectorPlan.create(
                "remote.test",
                (ConnectorStep.key(key),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
                profile=LG_TV_IR_PROFILE,
            )
            self.assertEqual(plan.steps, (ConnectorStep.key(key),))

        for key in ("NETFLIX", "SEARCH", "PLAY_PAUSE"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(TvConnectorError, "unsupported key"):
                    ConnectorPlan.create(
                        "remote.test",
                        (ConnectorStep.key(key),),
                        boot_id=TEST_CONNECTOR_BOOT_ID,
                        profile=LG_TV_IR_PROFILE,
                    )

        with self.assertRaisesRegex(TvConnectorError, "unavailable"):
            ConnectorPlan.create(
                "remote.text",
                (ConnectorStep.text("Netflix"),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
                profile=LG_TV_IR_PROFILE,
            )

    def test_connector_rejects_unknown_named_profile_locally(self):
        with self.assertRaisesRegex(TvConnectorError, "unsupported"):
            RecordingTvConnector(profile="unknown-tv-v1")
        with self.assertRaisesRegex(TvConnectorError, "unsupported"):
            ConnectorPlan.create(
                "remote.test",
                (ConnectorStep.key("POWER"),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
                profile="unknown-tv-v1",
            )

    def test_lg_profile_uses_distinct_play_and_pause_without_prior_session(self):
        client = RecordingTvConnector(profile=LG_TV_IR_PROFILE)

        paused = client.pause()
        resumed = client.resume()

        self.assertEqual(paused["media_status"], "Paused")
        self.assertEqual(resumed["media_status"], "Playing")
        self.assertEqual(
            client.executions,
            [
                ("media.pause", (ConnectorStep.key("PAUSE"),)),
                ("media.resume", (ConnectorStep.key("PLAY"),)),
            ],
        )

    def test_lg_profile_exposes_remote_controls_and_repeated_volume(self):
        client = RecordingTvConnector(profile=LG_TV_IR_PROFILE)

        client.power_on()
        client.power_off()
        client.home()
        client.back()
        client.input_source()
        client.navigate("down", count=2)
        client.select()
        client.mute()
        client.volume_up(3)
        client.volume_down(2)

        self.assertEqual(
            [purpose for purpose, _steps in client.executions],
            [
                "remote.power_on",
                "remote.power_off",
                "remote.home",
                "remote.back",
                "remote.input",
                "remote.down",
                "remote.enter",
                "remote.mute",
                "remote.volume_up",
                "remote.volume_down",
            ],
        )
        self.assertEqual(
            client.executions[-2][1],
            (ConnectorStep.key("VOLUME_UP"),) * 3,
        )
        self.assertEqual(
            client.executions[-1][1],
            (ConnectorStep.key("VOLUME_DOWN"),) * 2,
        )

    def test_lg_profile_rejects_apps_text_search_and_direct_youtube(self):
        client = RecordingTvConnector(profile=LG_TV_IR_PROFILE)

        with self.assertRaisesRegex(TvConnectorError, "no verified key"):
            client.launch_app("Netflix")
        with self.assertRaisesRegex(TvConnectorError, "Text search is unavailable"):
            client.search_google("weight of an elephant")
        with self.assertRaisesRegex(TvConnectorError, "TV online or an HDMI player"):
            client.play_youtube("abcdefghijk")
        self.assertEqual(client.executions, [])

    def test_lg_hybrid_profile_uses_ir_remote_keys_and_usb_app_search(self):
        client = RecordingTvConnector(profile=LG_WEBOS_HYBRID_PROFILE)

        opened = client.launch_app("YouTube")
        client.volume_up(2)
        client.navigate("right", count=2)

        self.assertEqual(opened["opened"], "YouTube")
        self.assertEqual(
            client.executions[0],
            (
                "app.open",
                (
                    ConnectorStep.key("HOME"),
                    ConnectorStep.wait(1_200),
                    ConnectorStep.key("SEARCH"),
                    ConnectorStep.wait(2_000),
                    ConnectorStep.text("YouTube"),
                    ConnectorStep.wait(1_200),
                    ConnectorStep.key("ENTER"),
                    ConnectorStep.wait(2_500),
                ),
            ),
        )
        self.assertEqual(
            client.executions[1],
            ("remote.volume_up", (ConnectorStep.key("VOLUME_UP"),) * 2),
        )
        self.assertEqual(
            client.executions[2],
            ("remote.right", (ConnectorStep.key("RIGHT"),) * 2),
        )

    def test_lg_hybrid_profile_can_type_and_select_a_youtube_video(self):
        client = RecordingTvConnector(profile=LG_WEBOS_HYBRID_PROFILE)

        result = client.play_youtube("abcdefghijk")

        self.assertEqual(result["opened"], "youtube:abcdefghijk")
        purpose, steps = client.executions[-1]
        self.assertEqual(purpose, "youtube.play")
        self.assertIn(ConnectorStep.text("YouTube"), steps)
        self.assertIn(ConnectorStep.text("abcdefghijk"), steps)
        self.assertEqual(steps.count(ConnectorStep.key("SEARCH")), 2)

    def test_lg_hybrid_profile_rejects_unverified_direct_app_keys(self):
        with self.assertRaisesRegex(TvConnectorError, "unsupported key"):
            ConnectorPlan.create(
                "app.open",
                (ConnectorStep.key("NETFLIX"),),
                boot_id=TEST_CONNECTOR_BOOT_ID,
                profile=LG_WEBOS_HYBRID_PROFILE,
            )

    def test_connector_client_executes_against_virtual_tv(self):
        secret = bytes.fromhex("6a" * 32)
        state = VirtualTvState()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_connector_simulator_handler(secret, state),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = UsbTvConnectorClient(
            f"http://127.0.0.1:{server.server_port}",
            secret,
        )
        try:
            health = client.health()
            opened = client.launch_app("Netflix")
            searched = client.search_google("weight of an elephant")
            first = client.open_first_google_result()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(health["device"], "virtual-atoms3u")
        self.assertEqual(opened["player"], "atoms3u-tv-connector")
        self.assertEqual(searched["connector_purpose"], "web.search")
        self.assertEqual(first["connector_purpose"], "web.open_first")
        self.assertEqual(state.query, "weight of an elephant")
        self.assertEqual(state.screen, "first_result")
        self.assertEqual(len(state.history), 3)

    def test_connector_rejects_a_signed_request_captured_before_reboot(self):
        secret = bytes.fromhex("6a" * 32)
        state = VirtualTvState(boot_id="22" * 16)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_connector_simulator_handler(secret, state),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        captured_plan = ConnectorPlan.create(
            "remote.mute",
            (ConnectorStep.key("MUTE"),),
            boot_id="11" * 16,
        )
        body = captured_plan.to_bytes()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/execute",
            data=body,
            headers=signed_headers(secret, body),
            method="POST",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()
            state.boot_id = "11" * 16
            with urllib.request.urlopen(request, timeout=2) as admitted:
                self.assertEqual(admitted.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(state.history), 1)
        self.assertTrue(state.muted)

    def test_connector_simulator_selects_each_supported_profile(self):
        parser = build_connector_simulator_parser()

        for profile in SUPPORTED_CONNECTOR_PROFILES:
            with self.subTest(profile=profile):
                args = parser.parse_args(
                    ["--key-file", "connector.key", "--profile", profile]
                )
                self.assertEqual(args.profile, profile)

    def test_connector_simulator_reports_exact_lg_profile_capabilities(self):
        secret = bytes.fromhex("6c" * 32)
        state = VirtualTvState()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_connector_simulator_handler(
                secret,
                state,
                profile=LG_TV_IR_PROFILE,
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = UsbTvConnectorClient(
            f"http://127.0.0.1:{server.server_port}",
            secret,
            profile=LG_TV_IR_PROFILE,
        )
        try:
            health = client.health()
            result = client.volume_up(2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(health["profile"], LG_TV_IR_PROFILE)
        self.assertEqual(
            health["capabilities"],
            sorted(CONNECTOR_KEYS_BY_PROFILE[LG_TV_IR_PROFILE]),
        )
        self.assertEqual(result["connector_profile"], LG_TV_IR_PROFILE)
        self.assertEqual(state.volume, 22)
        self.assertEqual(len(state.history), 1)

    def test_connector_simulator_rejects_signed_plan_for_another_profile(self):
        secret = bytes.fromhex("6d" * 32)
        state = VirtualTvState()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_connector_simulator_handler(
                secret,
                state,
                profile=LG_TV_IR_PROFILE,
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        mismatched_client = UsbTvConnectorClient(
            f"http://127.0.0.1:{server.server_port}",
            secret,
            profile=DEFAULT_CONNECTOR_PROFILE,
        )
        try:
            with self.assertRaisesRegex(TvConnectorError, "does not match") as rejected:
                mismatched_client.home()
            http_error = rejected.exception.__cause__
            if isinstance(http_error, urllib.error.HTTPError):
                http_error.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(state.history, [])
        self.assertEqual(state.screen, "home")

    def test_http_connector_timeout_includes_bounded_plan_waits(self):
        secret = bytes.fromhex("6b" * 32)
        opener = self._compatible_opener(secret)
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            timeout=8.0,
            opener=opener,
        )

        client._execute("test.wait", (ConnectorStep.wait(5_000),))

        self.assertEqual(opener.call_args.kwargs["timeout"], 13.0)

    def test_http_connector_rejects_legacy_firmware_before_execution(self):
        secret = bytes.fromhex("6a" * 32)
        opener = mock.Mock(return_value=self._health_response(version=1))
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            opener=opener,
        )

        with self.assertRaisesRegex(TvConnectorError, "reflash"):
            client.home()

        self.assertEqual(opener.call_count, 1)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/health"))

    def test_http_connector_rejects_float_protocol_version_in_health(self):
        secret = bytes.fromhex("6a" * 32)
        opener = mock.Mock(return_value=self._health_response(version=2.0))
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            opener=opener,
        )

        with self.assertRaisesRegex(TvConnectorError, "reflash"):
            client.home()
        self.assertEqual(opener.call_count, 1)

    def test_http_connector_requires_a_bare_origin(self):
        secret = bytes.fromhex("6a" * 32)
        for invalid_url in (
            "http://user@127.0.0.1:8780",
            "http://127.0.0.1:8780?mode=test",
            "http://127.0.0.1:8780#fragment",
            "http://127.0.0.1:8780/path",
            "http://localhost:abc",
            "http://[::1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:8780//",
            "http://127.0.0.1:8780\\health",
            "http://127.0.0.1:8780\nmalformed",
            "http://connector local",
            "http://česká.local",
        ):
            with self.subTest(url=invalid_url):
                with self.assertRaisesRegex(TvConnectorError, "HTTP.*origin"):
                    UsbTvConnectorClient(invalid_url, secret)

    def test_http_health_rejects_invalid_utf8(self):
        response_body = mock.MagicMock()
        response_body.__enter__.return_value.read.return_value = b"\xff"
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            bytes.fromhex("6a" * 32),
            opener=mock.Mock(return_value=response_body),
        )

        with self.assertRaisesRegex(TvConnectorError, "health check failed"):
            client.health()

    def test_http_health_rejects_oversized_response(self):
        payload = {
            "status": "ok",
            "version": CONNECTOR_VERSION,
            "profile": DEFAULT_CONNECTOR_PROFILE,
            "padding": "x" * MAX_CONNECTOR_BODY_BYTES,
        }
        response_body = mock.MagicMock()
        response_body.__enter__.return_value.read.return_value = json.dumps(payload).encode(
            "utf-8"
        )
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            bytes.fromhex("6a" * 32),
            opener=mock.Mock(return_value=response_body),
        )

        with self.assertRaisesRegex(TvConnectorError, "health response is too large"):
            client.health()

    def test_http_connector_rejects_unsigned_execution_response(self):
        secret = bytes.fromhex("6e" * 32)
        opener = self._compatible_opener(secret, signed=False)
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            opener=opener,
        )

        with self.assertRaisesRegex(TvConnectorError, "response signature"):
            client.home()

        self.assertEqual(opener.call_count, 2)
        probe_request = opener.call_args.args[0]
        probe_plan = ConnectorPlan.from_bytes(probe_request.data or b"")
        self.assertEqual(probe_plan.purpose, "connector.verify")
        self.assertEqual(probe_plan.steps, (ConnectorStep.wait(0),))

    def test_http_connector_rejects_signed_response_for_another_request(self):
        secret = bytes.fromhex("6f" * 32)
        opener = self._compatible_opener(
            secret,
            overrides={"request_id": str(uuid.uuid4())},
        )
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            opener=opener,
        )

        with self.assertRaisesRegex(TvConnectorError, "invalid execution response"):
            client.home()

    def test_http_connector_rejects_signed_float_protocol_version(self):
        secret = bytes.fromhex("6f" * 32)
        opener = self._compatible_opener(secret, overrides={"version": 2.0})
        client = UsbTvConnectorClient(
            "http://127.0.0.1:8780",
            secret,
            opener=opener,
        )

        with self.assertRaisesRegex(TvConnectorError, "invalid execution response"):
            client.home()

    def test_ble_connector_frames_preserve_signed_body_and_offsets(self):
        secret = bytes.fromhex("7b" * 32)
        body = ConnectorPlan.create(
            "media.pause",
            (ConnectorStep.key("PLAY_PAUSE"),),
            boot_id=TEST_CONNECTOR_BOOT_ID,
        ).to_bytes()

        frames = build_ble_frames(secret, body, chunk_size=16, now=1_786_500_000)

        frame_type, version, body_size, timestamp = struct.unpack("<BBHI", frames[0][:8])
        self.assertEqual(frame_type, BLE_FRAME_START)
        self.assertEqual(version, CONNECTOR_VERSION)
        self.assertEqual(body_size, len(body))
        self.assertEqual(timestamp, 1_786_500_000)
        nonce = frames[0][8:24].hex()
        signature = frames[0][24:56]
        expected = hmac.new(
            secret,
            f"{timestamp}\n{nonce}\n".encode("ascii") + body,
            hashlib.sha256,
        ).digest()
        self.assertEqual(signature, expected)
        rebuilt = bytearray()
        for frame in frames[1:-1]:
            self.assertEqual(frame[0], BLE_FRAME_DATA)
            self.assertEqual(struct.unpack("<H", frame[1:3])[0], len(rebuilt))
            rebuilt.extend(frame[3:])
        self.assertEqual(bytes(rebuilt), body)
        self.assertEqual(frames[-1], bytes((BLE_FRAME_END,)))

    def test_ble_connector_is_disabled_until_protocol_v2_is_authenticated(self):
        class FakeBleRuntime:
            def __init__(self):
                self.exchanges = []

            def health(self, timeout):
                return {"status": "ok", "transport": "ble-to-usb-hid"}

            def exchange(self, frames, request_id, timeout):
                self.exchanges.append((frames, request_id, timeout))
                return {"status": "executed", "request_id": request_id}

        runtime = FakeBleRuntime()
        with self.assertRaisesRegex(TvConnectorError, "disabled.*protocol v2"):
            BleTvConnectorClient(
                "atoms3u-tv-01",
                bytes.fromhex("4c" * 32),
                runtime=runtime,
            )
        self.assertEqual(runtime.exchanges, [])


class FakeTvConnector(FakeWindowsBrowser):
    def launch_app(self, app):
        self.calls.append(("launch_app", app))
        return {"opened": app, "player": "atoms3u-usb-tv-connector"}

    def power_off(self):
        self.calls.append(("power_off",))
        return {"connector_purpose": "remote.power_off"}

    def volume_up(self, count=1):
        self.calls.append(("volume_up", count))
        return {"connector_purpose": "remote.volume_up"}

    def navigate(self, direction, count=1):
        self.calls.append(("navigate", direction, count))
        return {"connector_purpose": f"remote.{direction}"}


class WindowsIphoneSessionTests(unittest.TestCase):
    def test_explicit_tv_youtube_open_uses_lg_hybrid_search_macro(self):
        browser = FakeWindowsBrowser()
        connector = RecordingTvConnector(profile=LG_WEBOS_HYBRID_PROFILE)
        session = WindowsIphoneSession(browser, tv_connector=connector)

        result = session.execute("Open YouTube on the TV")

        self.assertEqual(result["target"], "tv")
        self.assertEqual(result["executed_target"], "tv-connector")
        self.assertEqual(result["opened"], "YouTube")
        self.assertEqual(connector.executions[0][0], "app.open")
        self.assertIn(ConnectorStep.text("YouTube"), connector.executions[0][1])
        self.assertEqual(browser.calls, [])

    def test_configured_tv_connector_becomes_the_default_open_target(self):
        browser = FakeWindowsBrowser()
        connector = RecordingTvConnector(profile=LG_WEBOS_HYBRID_PROFILE)
        session = WindowsIphoneSession(browser, tv_connector=connector)

        result = session.execute("Open YouTube")

        self.assertEqual(result["target"], "tv")
        self.assertEqual(result["executed_target"], "tv-connector")
        self.assertEqual(result["opened"], "YouTube")
        self.assertEqual(connector.executions[0][0], "app.open")
        self.assertEqual(browser.calls, [])

    def test_follow_up_pause_controls_the_notebook_session(self):
        browser = FakeWindowsBrowser()
        session = WindowsIphoneSession(browser)

        opened = session.execute("Open YouTube")
        paused = session.execute("Pause the video")

        self.assertEqual(opened["active_target"], "notebook")
        self.assertEqual(paused["action"], "media.pause")
        self.assertEqual(browser.calls, [("open", "youtube"), ("pause",)])

    def test_youtube_follow_up_plays_latest_cinkatko_on_the_same_notebook(self):
        browser = FakeWindowsBrowser()
        media = MediaItem(
            "episode1234",
            "The newest Cinkatko episode",
            "2026-08-13T08:00:00Z",
            2_400,
            channel_title="Miton",
        )
        session = WindowsIphoneSession(
            browser,
            latest_resolver=lambda: media,
        )

        opened = session.execute("Open YouTube")
        played = session.execute(
            "Play the latest episode of Cinkatko podcast"
        )

        self.assertEqual(opened["action"], "web.open")
        self.assertEqual(opened["active_target"], "notebook")
        self.assertEqual(played["action"], "youtube.play_latest")
        self.assertEqual(played["target"], "notebook")
        self.assertEqual(played["executed_target"], "notebook")
        self.assertEqual(played["active_target"], "notebook")
        self.assertEqual(played["title"], "The newest Cinkatko episode")
        self.assertEqual(
            browser.calls,
            [("open", "youtube"), ("play_youtube", "episode1234")],
        )

    def test_youtube_follow_up_searches_and_plays_an_arbitrary_query(self):
        browser = FakeWindowsBrowser()
        queries = []
        media = MediaItem(
            "lofiTrack01",
            "lo-fi hip hop radio",
            "2026-08-13T08:00:00Z",
            3_600,
            channel_title="Lofi Girl",
        )

        def resolve(query):
            queries.append(query)
            return media

        session = WindowsIphoneSession(browser, video_resolver=resolve)

        session.execute("Open YouTube")
        played = session.execute("Play lo-fi hip hop")

        self.assertEqual(queries, ["lo-fi hip hop"])
        self.assertEqual(played["action"], "youtube.search_play")
        self.assertEqual(played["target"], "notebook")
        self.assertEqual(played["executed_target"], "notebook")
        self.assertEqual(played["active_target"], "notebook")
        self.assertEqual(played["title"], "lo-fi hip hop radio")
        self.assertEqual(
            browser.calls,
            [("open", "youtube"), ("play_youtube", "lofiTrack01")],
        )

    def test_non_youtube_browser_context_does_not_enable_generic_play(self):
        browser = FakeWindowsBrowser()
        session = WindowsIphoneSession(
            browser,
            search_resolver=lambda _query: self.fail(
                "A non-YouTube context must not invoke the YouTube resolver."
            ),
        )

        session.execute("Open Miton")

        with self.assertRaises(WindowsHostError):
            session.execute("Play lo-fi hip hop")
        self.assertEqual(browser.calls, [("open", "miton")])

    def test_tv_remote_command_does_not_steal_the_active_youtube_display(self):
        browser = FakeWindowsBrowser()
        connector = FakeTvConnector()
        media = MediaItem(
            "video123456",
            "The requested video",
            "2026-08-13T08:00:00Z",
            300,
        )
        session = WindowsIphoneSession(
            browser,
            video_resolver=lambda _query: media,
            tv_connector=connector,
        )

        session.execute("Open YouTube on the laptop")
        session.execute("Increase the volume")
        played = session.execute("Play an elephant documentary")

        self.assertEqual(played["target"], "notebook")
        self.assertEqual(played["active_target"], "notebook")
        self.assertEqual(
            browser.calls,
            [("open", "youtube"), ("play_youtube", "video123456")],
        )
        self.assertEqual(connector.calls, [("volume_up", 1)])

    def test_explicit_tv_target_can_alias_the_attached_windows_display(self):
        browser = FakeWindowsBrowser()
        session = WindowsIphoneSession(browser, treat_tv_as_local_display=True)

        result = session.execute("Open YouTube on the TV")

        self.assertEqual(result["target"], "tv")
        self.assertEqual(result["executed_target"], "notebook")

    def test_explicit_tv_target_is_rejected_without_an_enrolled_bridge(self):
        session = WindowsIphoneSession(FakeWindowsBrowser())

        with self.assertRaisesRegex(
            WindowsHostError,
            "configure the Wi-Fi ATOMS3U connector",
        ):
            session.execute("Open YouTube on the TV")

    def test_explicit_tv_target_uses_the_usb_connector_and_keeps_tv_context(self):
        browser = FakeWindowsBrowser()
        connector = FakeTvConnector()
        session = WindowsIphoneSession(browser, tv_connector=connector)

        opened = session.execute("Open Netflix on the TV")
        paused = session.execute("Pause")

        self.assertEqual(opened["executed_target"], "tv-connector")
        self.assertEqual(opened["active_target"], "tv")
        self.assertEqual(paused["executed_target"], "tv-connector")
        self.assertEqual(browser.calls, [])
        self.assertEqual(connector.calls, [("launch_app", "netflix"), ("pause",)])

    def test_iphone_voice_routes_remote_controls_to_connector(self):
        connector = FakeTvConnector()
        session = WindowsIphoneSession(FakeWindowsBrowser(), tv_connector=connector)

        powered = session.execute("Turn the TV off")
        louder = session.execute("Increase the volume by 10")
        moved = session.execute("Move right")

        self.assertEqual(powered["action"], "tv.remote")
        self.assertEqual(louder["executed_target"], "tv-connector")
        self.assertEqual(moved["active_target"], "tv")
        self.assertEqual(
            connector.calls,
            [("power_off",), ("volume_up", 10), ("navigate", "right", 1)],
        )


class FakeChromiumProcess:
    pid = 4242


class PiHdmiPlayerTests(unittest.TestCase):
    def test_room_canvas_home_requires_loopback_and_preloads_kiosk_extension(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=extension,
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
                home_url="http://127.0.0.1:8768/",
            )

            result = player.show_home()

        self.assertEqual(result["opened"], "http://127.0.0.1:8768/")
        self.assertIn(f"--load-extension={extension}", calls[0][0])

    def test_room_canvas_home_rejects_non_loopback_url(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                home_url="https://example.com/dashboard",
            )

    def test_google_search_launches_encoded_allowlisted_url(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=extension,
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
            )

            result = player.search_google("Miton AI")

        self.assertEqual(calls[0][0][-1], "https://www.google.com/search?q=Miton+AI")
        self.assertEqual(result["opened"], "https://www.google.com/search?q=Miton+AI")

    def test_first_google_result_reuses_query_and_marks_kiosk_action(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=extension,
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
            )

            player.search_google("Miton AI")
            result = player.open_first_google_result()

        self.assertEqual(
            calls[1][0][-1],
            "https://www.google.com/search?q=Miton+AI#room-agent-open-first",
        )
        self.assertEqual(result["opened"], calls[1][0][-1])

    def test_first_google_result_requires_prior_search(self):
        player = ChromiumHdmiPlayer(chromium="/usr/bin/chromium")
        with self.assertRaisesRegex(PiPlayerError, "no active Google search"):
            player.open_first_google_result()

    def test_miton_site_launches_without_youtube_helper(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=root / "missing-extension",
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
            )

            result = player.open_site("miton")

        command = calls[0][0]
        self.assertEqual(command[-1], "https://www.miton.cz/")
        self.assertFalse(any("load-extension" in argument for argument in command))
        self.assertEqual(result["opened"], "https://www.miton.cz/")

    def test_youtube_homepage_launches_kiosk_without_media_resume(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=extension,
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
            )

            result = player.open_youtube()

        self.assertEqual(calls[0][0][-1], "https://www.youtube.com/")
        self.assertEqual(result["opened"], "https://www.youtube.com/")
        self.assertNotIn("media_status", result)

    def test_valid_video_launches_kiosk_on_wayland(self):
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))
            return FakeChromiumProcess()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            extension = root / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                extension_dir=extension,
                runtime_dir=runtime,
                base_environment={},
                launcher=launcher,
                auto_play=False,
            )

            result = player.play_youtube("episode1234")

        command, kwargs = calls[0]
        self.assertIn("--kiosk", command)
        self.assertIn("--password-store=basic", command)
        self.assertIn(f"--disable-extensions-except={extension}", command)
        self.assertIn(f"--load-extension={extension}", command)
        self.assertEqual(
            command[-1],
            "https://www.youtube.com/watch?v=episode1234&autoplay=1",
        )
        self.assertEqual(kwargs["env"]["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(result["pid"], "4242")

    def test_native_media_controls_confirm_pause_and_resume(self):
        calls = []
        status = {"value": "Playing"}

        def runner(command, **kwargs):
            calls.append(command)
            if "--list" in command:
                output = (
                    "org.mpris.MediaPlayer2.chromium.instance4242 4242 chromium user :1.1 - -\n"
                )
            elif "get-property" in command:
                output = f's "{status["value"]}"\n'
            else:
                method = command[-1]
                status["value"] = "Paused" if method == "Pause" else "Playing"
                output = ""
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                runtime_dir=runtime,
                profile_dir=root / "profile",
                base_environment={},
                runner=runner,
                busctl="busctl",
                auto_play=False,
            )
            player.last_pid = 4242

            paused = player.pause()
            resumed = player.resume()

        self.assertEqual(paused["media_status"], "Paused")
        self.assertEqual(resumed["media_status"], "Playing")
        self.assertTrue(any(command[-1] == "Pause" for command in calls))
        self.assertTrue(any(command[-1] == "Play" for command in calls))

    def test_close_terminates_only_parent_using_dedicated_profile(self):
        killed = []

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile"
            proc = root / "proc"
            proc.mkdir()
            parent = proc / "4242"
            parent.mkdir()
            (parent / "cmdline").write_bytes(
                b"/usr/lib/chromium/chromium\0"
                + f"--user-data-dir={profile}".encode()
                + b"\0"
            )
            child = proc / "4243"
            child.mkdir()
            (child / "cmdline").write_bytes(
                b"/usr/lib/chromium/chromium\0--type=renderer\0"
                + f"--user-data-dir={profile}".encode()
                + b"\0"
            )

            def killer(pid, requested_signal):
                killed.append((pid, requested_signal))
                (proc / str(pid) / "cmdline").write_bytes(b"")

            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=profile,
                proc_root=proc,
                killer=killer,
                sleeper=lambda _seconds: None,
            )
            result = player.close()

        self.assertEqual(killed, [(4242, signal.SIGTERM)])
        self.assertEqual(result["media_status"], "Closed")
        self.assertEqual(result["closed_pids"], "4242")

    def test_close_terminates_launched_process_group_when_wrapper_pid_changes(self):
        killed_groups = []

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            proc = root / "proc"
            proc.mkdir()

            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=root / "profile",
                runtime_dir=runtime,
                proc_root=proc,
                base_environment={},
                launcher=lambda _command, **_kwargs: FakeChromiumProcess(),
                group_killer=lambda pid, requested_signal: killed_groups.append(
                    (pid, requested_signal)
                ),
            )
            player.open_site("miton")
            result = player.close()

        self.assertEqual(killed_groups, [(4242, signal.SIGTERM)])
        self.assertEqual(result["media_status"], "Closed")
        self.assertEqual(result["closed_process_groups"], "4242")

    def test_profile_scan_accepts_distribution_specific_chromium_name(self):
        killed = []

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile"
            proc = root / "proc"
            proc.mkdir()
            parent = proc / "4242"
            parent.mkdir()
            (parent / "cmdline").write_bytes(
                b"/usr/lib/chromium/chromium-bin\0"
                + f"--user-data-dir={profile}".encode()
                + b"\0"
            )

            def killer(pid, requested_signal):
                killed.append((pid, requested_signal))
                (proc / str(pid) / "cmdline").write_bytes(b"")

            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                profile_dir=profile,
                proc_root=proc,
                killer=killer,
                group_killer=lambda _pid, _signal: None,
                sleeper=lambda _seconds: None,
            )
            result = player.close()

        self.assertEqual(killed, [(4242, signal.SIGTERM)])
        self.assertEqual(result["closed_pids"], "4242")

    def test_typed_session_actions_call_local_media_controls(self):
        calls = []

        class FakePlayer:
            def pause(self):
                calls.append("pause")
                return {"media_status": "Paused"}

            def resume(self):
                calls.append("resume")
                return {"media_status": "Playing"}

            def close(self):
                calls.append("close")
                return {"media_status": "Closed"}

        player = FakePlayer()
        pause_action = route("Room Agent pause", active_target="tv")
        resume_action = route("Room Agent continue", active_target="tv")

        paused = execute_local_tv_action(
            pause_action,
            latest_resolver=None,
            player=player,
        )
        resumed = execute_local_tv_action(
            resume_action,
            latest_resolver=None,
            player=player,
        )
        close_action = route("close tab", active_target="tv")
        closed = execute_local_tv_action(
            close_action,
            latest_resolver=None,
            player=player,
        )

        self.assertEqual(calls, ["pause", "resume", "close"])
        self.assertEqual(paused["media_status"], "Paused")
        self.assertEqual(resumed["media_status"], "Playing")
        self.assertEqual(closed["media_status"], "Closed")

    def test_close_returns_to_room_canvas_when_configured(self):
        calls = []

        class FakePlayer:
            home_url = "http://127.0.0.1:8768/"

            def close(self):
                calls.append("close")
                return {"media_status": "Closed"}

            def show_home(self):
                calls.append("home")
                return {"opened": self.home_url}

        action = route("close tab", active_target="tv")
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=FakePlayer(),
        )

        self.assertEqual(calls, ["close", "home"])
        self.assertEqual(result["media_status"], "Closed")
        self.assertEqual(result["home"], "http://127.0.0.1:8768/")

    def test_typed_open_youtube_action_launches_local_homepage(self):
        opened = []

        class FakePlayer:
            def open_site(self, site):
                opened.append(site)
                return {
                    "opened": "https://www.youtube.com/",
                    "player": "fake",
                    "pid": "1",
                }

        action = route("Open YouTube", default_open_target="tv")
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=FakePlayer(),
        )

        self.assertEqual(opened, ["youtube"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["opened"], "https://www.youtube.com/")

    def test_typed_open_miton_action_launches_local_site(self):
        opened = []

        class FakePlayer:
            def open_site(self, site):
                opened.append(site)
                return {
                    "opened": "https://www.miton.cz/",
                    "player": "fake",
                    "pid": "1",
                }

        action = route("Open miton.cz", default_open_target="tv")
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=FakePlayer(),
        )

        self.assertEqual(opened, ["miton"])
        self.assertEqual(result["opened"], "https://www.miton.cz/")

    def test_typed_google_search_action_launches_local_results(self):
        searched = []

        class FakePlayer:
            def search_google(self, query):
                searched.append(query)
                return {
                    "opened": "https://www.google.com/search?q=Miton",
                    "player": "fake",
                    "pid": "1",
                }

        action = route("Google Miton", default_open_target="tv")
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=FakePlayer(),
        )

        self.assertEqual(searched, ["Miton"])
        self.assertEqual(result["provider"], "google")
        self.assertEqual(result["query"], "Miton")

    def test_typed_first_result_action_continues_local_search(self):
        calls = []

        class FakePlayer:
            def open_first_google_result(self):
                calls.append("first")
                return {
                    "opened": "https://example.com/",
                    "player": "fake",
                    "pid": "1",
                }

        action = route("Open first result", active_target="tv")
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=FakePlayer(),
        )

        self.assertEqual(calls, ["first"])
        self.assertEqual(result["action"], "web.open_first_result")

    def test_invalid_video_id_is_rejected_before_launch(self):
        player = ChromiumHdmiPlayer(chromium="/usr/bin/chromium")
        with self.assertRaises(PiPlayerError):
            player.play_youtube("not a video URL")

    def test_missing_kiosk_helper_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            player = ChromiumHdmiPlayer(
                chromium="/usr/bin/chromium",
                runtime_dir=runtime,
                extension_dir=root / "missing-extension",
            )
            with self.assertRaisesRegex(PiPlayerError, "kiosk helper is missing"):
                player.play_youtube("episode1234")

    def test_typed_tv_action_resolves_and_plays_locally(self):
        played = []
        media = MediaItem(
            video_id="episode1234",
            title="Newest full episode",
            published_at="2026-08-03T10:00:00Z",
            duration_seconds=2_700,
        )

        class FakePlayer:
            def play_youtube(self, video_id):
                played.append(video_id)
                return {"opened": media.url, "player": "fake", "pid": "1"}

        action = route("Play the latest Cinkatko podcast on the TV")
        result = execute_local_tv_action(
            action,
            latest_resolver=lambda: media,
            player=FakePlayer(),
        )

        self.assertEqual(played, ["episode1234"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["title"], "Newest full episode")

    def test_typed_tv_search_resolves_and_plays_locally(self):
        played = []
        queries = []
        media = MediaItem(
            video_id="episode1234",
            title="Newest Huberman Lab episode",
            published_at="2026-08-04T10:00:00Z",
            duration_seconds=5_400,
        )

        class FakePlayer:
            def play_youtube(self, video_id):
                played.append(video_id)
                return {"opened": media.url, "player": "fake", "pid": "1"}

        action = ActionRequest.create(
            "youtube.search_latest",
            "tv",
            {"query": "Huberman Lab", "selector": "latest_episode"},
        )
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            search_resolver=lambda query: queries.append(query) or media,
            player=FakePlayer(),
        )

        self.assertEqual(queries, ["Huberman Lab"])
        self.assertEqual(played, ["episode1234"])
        self.assertEqual(result["title"], "Newest Huberman Lab episode")

    def test_typed_tv_video_search_resolves_and_plays_locally(self):
        played = []
        queries = []
        media = MediaItem(
            video_id="video123456",
            title="An elephant documentary",
            published_at="2026-08-04T10:00:00Z",
            duration_seconds=900,
        )

        class FakePlayer:
            def play_youtube(self, video_id):
                played.append(video_id)
                return {"opened": media.url, "player": "fake", "pid": "1"}

        action = ActionRequest.create(
            "youtube.search_play",
            "tv",
            {"query": "elephant documentary"},
        )
        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            video_resolver=lambda query: queries.append(query) or media,
            player=FakePlayer(),
        )

        self.assertEqual(queries, ["elephant documentary"])
        self.assertEqual(played, ["video123456"])
        self.assertEqual(result["title"], "An elephant documentary")

    def test_tv_action_requires_local_youtube_configuration(self):
        action = route("Play the latest Cinkatko podcast on the TV")
        with self.assertRaises(HubError):
            execute_local_tv_action(
                action,
                latest_resolver=None,
                player=object(),
            )


class FakeHdmiPlayer:
    home_url = None

    def __init__(self):
        self.calls = []

    def is_available(self):
        return True, "test HDMI ready"

    def open_site(self, site):
        self.calls.append(("open_site", site))
        return {"opened": site, "player": "fake-hdmi"}

    def search_google(self, query):
        self.calls.append(("search_google", query))
        return {"opened": query, "player": "fake-hdmi"}

    def open_first_google_result(self):
        self.calls.append(("open_first",))
        return {"opened": "first", "player": "fake-hdmi"}

    def play_youtube(self, video_id):
        self.calls.append(("play_youtube", video_id))
        return {"opened": video_id, "player": "fake-hdmi"}

    def pause(self):
        self.calls.append(("pause",))
        return {"media_status": "Paused", "player": "fake-hdmi"}

    def resume(self):
        self.calls.append(("resume",))
        return {"media_status": "Playing", "player": "fake-hdmi"}

    def close(self):
        self.calls.append(("close",))
        return {"media_status": "Closed", "player": "fake-hdmi"}


class FakeNativeEndpoint(DisplayEndpoint):
    kind = "test_native"

    def __init__(
        self,
        capabilities,
        *,
        fail=None,
        priority=100,
        endpoint_id="native",
        name="Native Test TV",
    ):
        super().__init__(endpoint_id, name, priority)
        self.capabilities = frozenset(capabilities)
        self.fail = fail
        self.calls = []
        self.probes = 0

    def probe(self):
        self.probes += 1
        return EndpointStatus(
            self.endpoint_id,
            self.name,
            self.kind,
            True,
            self.capabilities,
            "test native ready",
        )

    def _call(self, operation, value=None):
        self.calls.append((operation, value))
        if self.fail == operation:
            raise DisplayControlError(f"native {operation} failed")
        return {"native_operation": operation, "player": "fake-native"}

    def open_site(self, site):
        return self._call("open_site", site)

    def launch_app(self, app):
        return self._call("launch_app", app)

    def search_google(self, query):
        return self._call("search_google", query)

    def play_youtube(self, video_id):
        return self._call("play_youtube", video_id)

    def pause(self):
        return self._call("pause")

    def resume(self):
        return self._call("resume")

    def close(self):
        return self._call("close")


class SamsungTizenHelperTests(unittest.TestCase):
    def test_compacts_current_samsung_installed_app_shape(self):
        apps = _compact_apps(
            [
                {"appId": "11101200001", "name": "Netflix", "isVisible": True},
                {"appId": "bad id", "name": "Rejected"},
                {"name": "Missing id"},
            ]
        )

        self.assertEqual(
            apps,
            [{"id": "11101200001", "title": "Netflix", "visible": True}],
        )

    def test_samsung_browser_rejects_arbitrary_model_urls(self):
        self.assertEqual(
            _allowed_web_url("https://www.youtube.com/watch?v=episode1234"),
            "https://www.youtube.com/watch?v=episode1234",
        )
        with self.assertRaises(SamsungTizenError):
            _allowed_web_url("https://example.com/model-controlled")


class DisplayManagerTests(unittest.TestCase):
    def test_native_youtube_is_preferred_over_pi_hdmi(self):
        hdmi = FakeHdmiPlayer()
        native = FakeNativeEndpoint({CAP_APP_YOUTUBE})
        manager = DisplayManager(hdmi, [native])

        result = manager.open_site("youtube")

        self.assertEqual(result["display_endpoint"], "native")
        self.assertEqual(native.calls, [("open_site", "youtube")])
        self.assertEqual(hdmi.calls, [])

    def test_successful_high_priority_endpoint_skips_slower_secondary_probe(self):
        primary = FakeNativeEndpoint({CAP_APP_YOUTUBE}, priority=100)
        secondary = FakeNativeEndpoint(
            {CAP_APP_YOUTUBE},
            priority=90,
            endpoint_id="secondary",
            name="Secondary TV",
        )
        manager = DisplayManager(FakeHdmiPlayer(), [primary, secondary])

        manager.open_site("youtube")

        self.assertEqual(primary.probes, 1)
        self.assertEqual(secondary.probes, 0)

    def test_web_search_uses_hdmi_when_smart_endpoint_has_no_browser(self):
        hdmi = FakeHdmiPlayer()
        native = FakeNativeEndpoint({CAP_APP_YOUTUBE, CAP_APP_NETFLIX})
        manager = DisplayManager(hdmi, [native])

        result = manager.search_google("weight of an elephant")

        self.assertEqual(result["display_kind"], "pi_hdmi")
        self.assertEqual(hdmi.calls, [("search_google", "weight of an elephant")])

    def test_failed_native_launch_falls_back_to_hdmi_for_same_capability(self):
        hdmi = FakeHdmiPlayer()
        native = FakeNativeEndpoint({CAP_YOUTUBE_PLAY}, fail="play_youtube")
        manager = DisplayManager(hdmi, [native])

        result = manager.play_youtube("episode1234")

        self.assertEqual(result["display_kind"], "pi_hdmi")
        self.assertEqual(result["fallback_from"], "Native Test TV")
        self.assertIn("native play_youtube failed", result["fallback_reason"])

    def test_media_controls_stay_with_the_endpoint_that_started_playback(self):
        hdmi = FakeHdmiPlayer()
        native = FakeNativeEndpoint(
            {
                CAP_YOUTUBE_PLAY,
                CAP_MEDIA_PAUSE,
                CAP_MEDIA_RESUME,
                CAP_MEDIA_CLOSE,
            }
        )
        manager = DisplayManager(hdmi, [native])

        manager.play_youtube("episode1234")
        paused = manager.pause()
        closed = manager.close()

        self.assertEqual(paused["display_endpoint"], "native")
        self.assertEqual(closed["display_endpoint"], "native")
        self.assertIsNone(manager.active_endpoint_id)
        self.assertEqual(hdmi.calls, [])

    def test_media_failure_does_not_jump_to_a_different_screen(self):
        native = FakeNativeEndpoint(
            {CAP_YOUTUBE_PLAY, CAP_MEDIA_PAUSE}, fail="pause"
        )
        manager = DisplayManager(FakeHdmiPlayer(), [native])
        manager.play_youtube("episode1234")

        with self.assertRaisesRegex(DisplayControlError, "native pause failed"):
            manager.pause()

    def test_missing_configuration_preserves_hdmi_only_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = load_display_manager(Path(temp) / "missing.json", FakeHdmiPlayer())

        statuses = manager.statuses(force=True)
        self.assertEqual([status.kind for status in statuses], ["pi_hdmi"])

    def test_samsung_tizen_discovers_apps_browser_and_remote_controls(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            operation = command[2]
            if operation == "probe":
                payload = {
                    "status": "ready",
                    "apps": [
                        {
                            "id": "111299001912",
                            "title": "YouTube",
                            "visible": True,
                        },
                        {
                            "id": "11101200001",
                            "title": "Netflix",
                            "visible": True,
                        },
                        {
                            "id": "org.tizen.browser",
                            "title": "Internet Browser",
                            "visible": True,
                        },
                    ],
                }
            else:
                payload = {"status": "executed"}
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload) + "\n", stderr=""
            )

        with tempfile.TemporaryDirectory() as temp:
            token_file = Path(temp) / "samsung-token.txt"
            token_file.write_text("paired", encoding="utf-8")
            endpoint = SamsungTvEndpoint(
                "samsung",
                "Samsung Room TV",
                105,
                host="10.0.0.20",
                port=8002,
                token_file=str(token_file),
                python="python3",
                helper="samsung_tizen.py",
                runner=runner,
            )

            status = endpoint.probe()
            netflix = endpoint.launch_app("netflix")
            search = endpoint.search_google("weight of an elephant")
            video = endpoint.play_youtube("episode1234")
            paused = endpoint.pause()
            resumed = endpoint.resume()
            closed = endpoint.close()

        self.assertTrue(status.online)
        self.assertTrue(
            {
                CAP_APP_YOUTUBE,
                CAP_APP_NETFLIX,
                CAP_WEB_BROWSER,
                CAP_WEB_SEARCH,
                CAP_YOUTUBE_PLAY,
                CAP_MEDIA_PAUSE,
                CAP_MEDIA_RESUME,
                CAP_MEDIA_CLOSE,
            }
            <= status.capabilities
        )
        self.assertEqual(netflix["app_id"], "11101200001")
        self.assertIn("weight+of+an+elephant", search["opened"])
        self.assertEqual(video["player"], "samsung-tizen-browser")
        self.assertEqual(paused["media_status"], "PauseRequested")
        self.assertEqual(resumed["media_status"], "PlayRequested")
        self.assertEqual(closed["media_status"], "Home")
        self.assertEqual(
            [command[2] for command in calls],
            ["probe", "launch-app", "open-url", "youtube", "key", "key", "key"],
        )

    def test_samsung_secure_probe_requires_existing_pairing_token(self):
        calls = []
        endpoint = SamsungTvEndpoint(
            "samsung",
            "Samsung Room TV",
            105,
            host="10.0.0.20",
            port=8002,
            token_file="missing-samsung-token.txt",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        status = endpoint.probe()

        self.assertFalse(status.online)
        self.assertIn("pairing token is missing", status.detail)
        self.assertEqual(calls, [])

    def test_lg_webos_discovers_apps_browser_youtube_and_media_controls(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            operation = command[2]
            if operation == "probe":
                payload = {
                    "status": "ready",
                    "apps": [
                        {
                            "id": "youtube.leanback.v4",
                            "title": "YouTube",
                            "visible": True,
                        },
                        {
                            "id": "com.webos.app.netflix",
                            "title": "Netflix",
                            "visible": True,
                        },
                        {
                            "id": "com.webos.app.browser",
                            "title": "Web Browser",
                            "visible": True,
                        },
                    ],
                }
            else:
                payload = {"status": "executed"}
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload) + "\n", stderr=""
            )

        with tempfile.TemporaryDirectory() as temp:
            key_file = Path(temp) / "lg.sqlite"
            key_file.write_text("paired", encoding="utf-8")
            endpoint = LgWebOsEndpoint(
                "lg_tv",
                "Living Room LG",
                110,
                host="10.0.0.55",
                key_file=str(key_file),
                python="python3",
                helper="lg_webos.py",
                runner=runner,
            )

            status = endpoint.probe()
            netflix = endpoint.launch_app("netflix")
            search = endpoint.search_google("weight of an elephant")
            video = endpoint.play_youtube("episode1234")
            paused = endpoint.pause()
            resumed = endpoint.resume()
            closed = endpoint.close()

        self.assertTrue(status.online)
        self.assertTrue(
            {
                CAP_APP_YOUTUBE,
                CAP_APP_NETFLIX,
                CAP_WEB_BROWSER,
                CAP_WEB_SEARCH,
                CAP_YOUTUBE_PLAY,
                CAP_MEDIA_PAUSE,
                CAP_MEDIA_RESUME,
                CAP_MEDIA_CLOSE,
            }
            <= status.capabilities
        )
        self.assertEqual(netflix["app_id"], "com.webos.app.netflix")
        self.assertIn("weight+of+an+elephant", search["opened"])
        self.assertEqual(video["app_id"], "youtube.leanback.v4")
        self.assertEqual(paused["media_status"], "PauseRequested")
        self.assertEqual(resumed["media_status"], "PlayRequested")
        self.assertEqual(closed["media_status"], "Home")
        self.assertEqual(
            [command[2] for command in calls],
            ["probe", "launch-app", "open-url", "youtube", "pause", "resume", "home"],
        )

    def test_lg_webos_missing_pairing_key_does_not_prompt_during_probe(self):
        calls = []
        endpoint = LgWebOsEndpoint(
            "lg_tv",
            "Living Room LG",
            110,
            host="10.0.0.55",
            key_file="missing-lg-key.sqlite",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        status = endpoint.probe()

        self.assertFalse(status.online)
        self.assertIn("pairing key is missing", status.detail)
        self.assertEqual(calls, [])

    def test_lg_ssdp_discovery_filters_non_webos_devices(self):
        class FakeSocket:
            def __init__(self):
                self.responses = [
                    (
                        b"HTTP/1.1 200 OK\r\n"
                        b"ST: urn:lge-com:service:webos-second-screen:1\r\n"
                        b"USN: uuid:lg-tv::urn:lge-com:service:webos-second-screen:1\r\n\r\n",
                        ("10.0.0.55", 1900),
                    ),
                    (
                        b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:Printer:1\r\n\r\n",
                        ("10.0.0.90", 1900),
                    ),
                ]
                self.sent = []
                self.closed = False

            def settimeout(self, _timeout):
                pass

            def sendto(self, data, address):
                self.sent.append((data, address))

            def recvfrom(self, _size):
                if self.responses:
                    return self.responses.pop(0)
                raise socket.timeout()

            def close(self):
                self.closed = True

        fake = FakeSocket()
        tick = {"value": 0.0}

        def clock():
            tick["value"] += 0.05
            return tick["value"]

        devices = discover_lg_webos(
            0.3,
            socket_factory=lambda *_args: fake,
            clock=clock,
        )

        self.assertEqual([device["host"] for device in devices], ["10.0.0.55"])
        self.assertIn(b"webos-second-screen", fake.sent[0][0])
        self.assertTrue(fake.closed)

    def test_android_tv_probe_verifies_apps_browser_and_deep_links(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            joined = " ".join(command)
            if command[1:3] == ["devices", "-l"]:
                output = "List of devices attached\n10.0.0.30:5555 device product:x model:Streamer\n"
            elif "getprop ro.product.model" in joined:
                output = "Google TV Streamer\n"
            elif "pm path com.google.android.youtube.tv" in joined:
                output = "package:/data/app/youtube/base.apk\n"
            elif "pm path com.netflix.ninja" in joined:
                output = "package:/data/app/netflix/base.apk\n"
            elif "resolve-activity" in command:
                output = "com.android.browser/.BrowserActivity\n"
            elif "dumpsys window windows" in joined:
                output = (
                    "mCurrentFocus=com.netflix.ninja/.MainActivity "
                    "com.google.android.youtube.tv/.MainActivity "
                    "com.android.browser/.BrowserActivity\n"
                )
            elif "dumpsys media_session" in joined:
                output = "state=PlaybackState {state=3, position=0}\n"
            elif "am start" in joined:
                output = "Starting: Intent\nStatus: ok\n"
            elif "monkey" in command:
                output = "Events injected: 1\n"
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        endpoint = AndroidTvEndpoint(
            "google_tv",
            "Google TV Streamer",
            100,
            serial="10.0.0.30:5555",
            runner=runner,
        )

        status = endpoint.probe()
        app = endpoint.launch_app("netflix")
        video = endpoint.play_youtube("episode1234")

        self.assertTrue(status.online)
        self.assertTrue(
            {CAP_APP_YOUTUBE, CAP_APP_NETFLIX, CAP_WEB_SEARCH, CAP_YOUTUBE_PLAY}
            <= status.capabilities
        )
        self.assertEqual(app["package"], "com.netflix.ninja")
        self.assertEqual(video["player"], "android-tv-adb")
        self.assertTrue(any("android.intent.extra.START_PLAYBACK" in call for call in calls))

    def test_apple_tv_probe_uses_actual_installed_app_list(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            if command[-1] == "app_list":
                output = (
                    "App: Netflix (com.netflix.Netflix), "
                    "App: YouTube (com.google.ios.youtube)\n"
                )
            else:
                output = "Command executed\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        endpoint = AppleTvEndpoint(
            "apple_tv",
            "Apple TV",
            90,
            host="10.0.0.40",
            identifier="91CFB12F-9069-4D8F-B022-5528DB833611",
            atvremote="atvremote",
            runner=runner,
        )

        status = endpoint.probe()
        result = endpoint.launch_app("netflix")

        self.assertTrue(status.online)
        self.assertIn(CAP_APP_NETFLIX, status.capabilities)
        self.assertNotIn(CAP_YOUTUBE_PLAY, status.capabilities)
        self.assertEqual(result["bundle_id"], "com.netflix.Netflix")
        self.assertIn("--protocol", calls[0])
        self.assertEqual(calls[0][calls[0].index("--protocol") + 1], "companion")
        self.assertIn("launch_app=com.netflix.Netflix", calls[-1])

    def test_android_media_controls_require_confirmed_session_state(self):
        media_state = {"value": 3}

        def runner(command, **_kwargs):
            joined = " ".join(command)
            if command[1:3] == ["devices", "-l"]:
                output = "List of devices attached\n10.0.0.30:5555 device model:Streamer\n"
            elif "getprop ro.product.model" in joined:
                output = "Google TV Streamer\n"
            elif "KEYCODE_MEDIA_PAUSE" in command:
                media_state["value"] = 2
                output = ""
            elif "KEYCODE_MEDIA_PLAY" in command:
                media_state["value"] = 3
                output = ""
            elif "dumpsys media_session" in joined:
                output = f"state=PlaybackState {{state={media_state['value']}}}\n"
            else:
                output = "No activity found\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        endpoint = AndroidTvEndpoint(
            "google_tv",
            "Google TV Streamer",
            100,
            serial="10.0.0.30:5555",
            runner=runner,
        )
        endpoint.probe()

        paused = endpoint.pause()
        resumed = endpoint.resume()

        self.assertEqual(paused["media_status"], "Paused")
        self.assertEqual(resumed["media_status"], "Playing")

    def test_android_tv_recovers_a_changed_wireless_debugging_port_via_mdns(self):
        connected = {"serial": None}

        def runner(command, **_kwargs):
            if command[1:3] == ["devices", "-l"]:
                device = (
                    f"{connected['serial']} device model:Streamer\n"
                    if connected["serial"]
                    else ""
                )
                output = "List of devices attached\n" + device
            elif command[1:3] == ["mdns", "services"]:
                output = (
                    "List of discovered mdns services\n"
                    "adb-room _adb-tls-connect._tcp 10.0.0.30:43821\n"
                )
            elif command[1:2] == ["connect"]:
                if command[-1].endswith(":43821"):
                    connected["serial"] = command[-1]
                output = f"connected to {command[-1]}\n"
            elif "getprop" in command:
                output = "Google TV Streamer\n"
            else:
                output = "No activity found\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        endpoint = AndroidTvEndpoint(
            "google_tv",
            "Google TV Streamer",
            100,
            serial="10.0.0.30:5555",
            host="10.0.0.30",
            runner=runner,
        )

        status = endpoint.probe()

        self.assertTrue(status.online)
        self.assertIn("10.0.0.30:43821", status.detail)

    def test_typed_netflix_action_uses_native_app_executor(self):
        action = route("Open Netflix on the TV", default_open_target="tv")
        launched = []

        class Player:
            def launch_app(self, app):
                launched.append(app)
                return {"app": app, "player": "native"}

        result = execute_local_tv_action(
            action,
            latest_resolver=None,
            player=Player(),
        )

        self.assertEqual(action.action, "app.open")
        self.assertEqual(launched, ["netflix"])
        self.assertEqual(result["status"], "executed")


class HandsFreeCaptureTests(unittest.TestCase):
    def test_capture_pipeline_uses_processed_channel_and_bounded_silence(self):
        recorder, processor = hands_free_commands(
            "plughw:CARD=ArrayUAC10,DEV=0",
            Path("command.wav"),
            max_seconds=12,
            silence_seconds=1.2,
            threshold_percent=2.0,
        )

        self.assertEqual(recorder[0], "arecord")
        self.assertIn("plughw:CARD=ArrayUAC10,DEV=0", recorder)
        self.assertEqual(processor[0], "sox")
        self.assertIn("remix", processor)
        self.assertEqual(processor[processor.index("remix") + 1], "1")
        self.assertIn("silence", processor)
        self.assertIn("2%", processor)
        self.assertEqual(processor[-3:], ["trim", "0", "12"])


class TextConsoleTests(unittest.TestCase):
    def test_console_skips_directives_and_returns_next_room_command(self):
        commands = iter(["", ":status", ":help", "  Google Miton  "])
        output = []

        command = prompt_text_console(
            "tv",
            input_fn=lambda _prompt: next(commands),
            output=output.append,
        )

        self.assertEqual(command, "Google Miton")
        self.assertEqual(output[0], "Active target: tv")
        self.assertIn(":exit", output[1])

    def test_console_exit_is_separate_from_spoken_quit_command(self):
        self.assertIsNone(
            prompt_text_console(None, input_fn=lambda _prompt: ":exit")
        )
        self.assertEqual(
            prompt_text_console("tv", input_fn=lambda _prompt: "quit"),
            "quit",
        )

    def test_console_eof_closes_cleanly(self):
        def eof(_prompt):
            raise EOFError

        self.assertIsNone(prompt_text_console(None, input_fn=eof))


class SharedCommandSessionTests(unittest.TestCase):
    def test_iphone_and_respeaker_commands_share_one_serial_session(self):
        state = StateMachine()
        state.transition(AgentState.READY)
        session = CommandSession(
            object(), state, None, None, None, object(), None
        )
        seen_targets = []
        failures = []
        maximum_parallel = 0
        active_calls = 0
        counter_lock = threading.Lock()

        def fake_attempt(
            _args,
            current_state,
            _secret,
            _latest,
            _search,
            _player,
            active_target,
            _router,
            **_kwargs,
        ):
            nonlocal active_calls, maximum_parallel
            with counter_lock:
                active_calls += 1
                maximum_parallel = max(maximum_parallel, active_calls)
            seen_targets.append(active_target)
            current_state.transition(AgentState.ROUTING)
            current_state.transition(AgentState.EXECUTING)
            time.sleep(0.03)
            current_state.transition(AgentState.SUCCESS)
            with counter_lock:
                active_calls -= 1
            return CommandOutcome("tv", "media.pause", "tv", {"status": "executed"})

        def execute(text):
            try:
                session.execute(text)
            except Exception as exc:
                failures.append(exc)

        with mock.patch("pi_hub.run_attempt", side_effect=fake_attempt):
            threads = [
                threading.Thread(target=execute, args=(source,))
                for source in ("iPhone", "ReSpeaker")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(failures, [])
        self.assertEqual(maximum_parallel, 1)
        self.assertEqual(seen_targets, [None, "tv"])
        self.assertEqual(session.active_target, "tv")


class HttpIntegrationTests(unittest.TestCase):
    def test_iphone_gateway_port_is_single_instance_and_reusable_after_close(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        first = IphoneCommandServer(
            b"1" * 32,
            lambda transcript: {"active_target": "tv"},
            port=port,
        )
        try:
            with self.assertRaises(OSError):
                IphoneCommandServer(
                    b"2" * 32,
                    lambda transcript: {"active_target": "tv"},
                    port=port,
                )
        finally:
            first.close()

        replacement = IphoneCommandServer(
            b"3" * 32,
            lambda transcript: {"active_target": "tv"},
            port=port,
        )
        replacement.close()

    def test_iphone_response_contains_a_human_friendly_shortcut_card(self):
        response = add_shortcut_presentation(
            {
                "status": "executed",
                "action": "youtube.search_latest",
                "executed_target": "tv-connector",
                "title": "The newest podcast episode",
                "duration_ms": 1_840,
            }
        )

        self.assertEqual(response["shortcut_card"]["title"], "Now playing")
        self.assertEqual(response["shortcut_card"]["detail"], "The newest podcast episode")
        self.assertEqual(response["shortcut_card"]["subtitle"], "Television  •  1.8 s")
        self.assertEqual(
            response["popup"],
            "▶️  Now playing\nThe newest podcast episode\nTelevision  •  1.8 s",
        )

    def test_iphone_error_response_contains_a_safe_failure_card(self):
        response = add_shortcut_presentation(
            {
                "status": "error",
                "error": "command_failed",
                "message": "The television is offline.",
            }
        )

        self.assertEqual(response["shortcut_card"]["title"], "Couldn’t complete that")
        self.assertIn("The television is offline.", response["popup"])

    def test_iphone_shortcut_accepts_current_date_as_request_id(self):
        first = parse_command(
            json.dumps(
                {
                    "version": 1,
                    "request_id": "7 August 2026 at 12:34:56",
                    "text": "Open YouTube",
                }
            ).encode()
        )
        repeated = parse_command(
            json.dumps(
                {
                    "version": 1,
                    "request_id": "7 August 2026 at 12:34:56",
                    "text": "Open YouTube",
                }
            ).encode()
        )

        self.assertEqual(first.request_id, repeated.request_id)
        self.assertEqual(str(uuid.UUID(first.request_id)), first.request_id)

    def test_iphone_shortcut_posts_an_authenticated_normalized_transcript(self):
        secret = bytes.fromhex("44" * 32)
        received = []
        request_id = "8f39dc0f-f43d-4d89-b3bf-6bf557f80e65"

        def execute(transcript):
            received.append(transcript)
            return {"active_target": "tv"}

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_iphone_handler(secret, execute)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "version": 1,
                    "request_id": request_id,
                    "text": "  Open   YouTube on the TV  ",
                }
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/commands",
                data=body,
                headers={
                    "Authorization": f"Bearer {secret.hex()}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(received, ["Open YouTube on the TV"])
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["active_target"], "tv")
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["version"], 1)

    def test_iphone_shortcut_rejects_a_missing_bearer_token(self):
        secret = bytes.fromhex("55" * 32)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_iphone_handler(secret, lambda _text: {"active_target": "tv"}),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/commands",
                data=(
                    b'{"version":1,"request_id":'
                    b'"78b064f1-63b1-4ad2-9dc1-fcd1fd3d111c",'
                    b'"text":"Open YouTube"}'
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(rejected.exception.code, 401)

    def test_iphone_transcript_schema_rejects_model_supplied_fields(self):
        with self.assertRaises(IphoneGatewayError):
            parse_transcript(
                b'{"version":1,"request_id":'
                b'"78b064f1-63b1-4ad2-9dc1-fcd1fd3d111c",'
                b'"text":"Open YouTube","target":"arbitrary"}'
            )

    def test_iphone_enrollment_is_hashed_and_revocation_is_immediate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "iphone-devices.json"
            registry = IphoneDeviceRegistry(path)
            enrollment = registry.enroll("Test iPhone")

            stored = path.read_text(encoding="utf-8")
            self.assertNotIn(enrollment.token, stored)
            self.assertEqual(
                registry.authenticate(enrollment.token).device_id,
                enrollment.device_id,
            )

            registry.revoke(enrollment.device_id)
            self.assertIsNone(registry.authenticate(enrollment.token))

    def test_iphone_token_rotation_invalidates_the_previous_token(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = IphoneDeviceRegistry(Path(directory) / "devices.json")
            initial = registry.enroll("Demo phone")
            rotated = registry.rotate(initial.device_id)

            self.assertIsNone(registry.authenticate(initial.token))
            self.assertEqual(
                registry.authenticate(rotated.token).device_id,
                initial.device_id,
            )

    def test_app_pairing_code_is_hashed_one_use_and_issues_a_device_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            registry = IphoneDeviceRegistry(path)
            invitation = registry.create_pairing("Test iPhone", ttl_seconds=600)

            stored_before = path.read_text(encoding="utf-8")
            self.assertNotIn(invitation.code, stored_before)
            enrollment = registry.redeem_pairing(invitation.code)

            self.assertEqual(enrollment.name, "Test iPhone")
            self.assertEqual(
                registry.authenticate(enrollment.token).device_id,
                enrollment.device_id,
            )
            stored_after = path.read_text(encoding="utf-8")
            self.assertNotIn(enrollment.token, stored_after)
            with self.assertRaises(IphoneGatewayError):
                registry.redeem_pairing(invitation.code)

    def test_expired_app_pairing_code_cannot_be_redeemed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            registry = IphoneDeviceRegistry(path)
            invitation = registry.create_pairing("Expired iPhone", ttl_seconds=30)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pairings"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                IphoneGatewayError,
                "invalid or expired",
            ):
                registry.redeem_pairing(invitation.code)
            self.assertEqual(registry.list_devices(), [])

    def test_parallel_app_pairing_redemption_issues_exactly_one_token(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = IphoneDeviceRegistry(Path(directory) / "devices.json")
            invitation = registry.create_pairing("Parallel iPhone")
            barrier = threading.Barrier(3)
            enrollments = []
            failures = []

            def redeem():
                barrier.wait(timeout=2)
                try:
                    enrollments.append(registry.redeem_pairing(invitation.code))
                except Exception as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=redeem) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(enrollments), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], IphoneGatewayError)
            self.assertEqual(len(registry.list_devices()), 1)
            self.assertEqual(
                registry.authenticate(enrollments[0].token).device_id,
                enrollments[0].device_id,
            )

    def test_legacy_registry_without_pairings_is_migrated_without_token_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            registry = IphoneDeviceRegistry(path)
            device_id = "0123456789abcdef"
            existing_token = f"ra1.{device_id}.{'A' * 43}"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "devices": [
                            {
                                "id": device_id,
                                "name": "Existing Shortcut",
                                "token_sha256": hashlib.sha256(
                                    existing_token.encode("ascii")
                                ).hexdigest(),
                                "created_at": "2026-08-01T10:00:00+00:00",
                                "revoked_at": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                registry.authenticate(existing_token).device_id,
                device_id,
            )
            invitation = registry.create_pairing("Native app")
            enrollment = registry.redeem_pairing(invitation.code)

            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["version"], 1)
            self.assertEqual(len(migrated["devices"]), 2)
            self.assertEqual(migrated["pairings"], [])
            self.assertEqual(
                registry.authenticate(existing_token).device_id,
                device_id,
            )
            self.assertEqual(
                registry.authenticate(enrollment.token).device_id,
                enrollment.device_id,
            )

    def test_app_pairing_link_generates_a_parseable_svg_qr(self):
        link = _pairing_link(
            "https://desktop.example.ts.net",
            f"rap1.0123456789abcdef.{'B' * 43}",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "room-agent-pairing.svg"
            saved = _save_qr(link, output)

            self.assertEqual(saved, output.resolve())
            self.assertGreater(saved.stat().st_size, 1_000)
            root = ElementTree.parse(saved).getroot()
            self.assertTrue(root.tag.endswith("svg"))
            self.assertTrue(any(element.tag.endswith("path") for element in root.iter()))

    def test_manual_shortcut_token_requires_explicit_reveal(self):
        with mock.patch("builtins.print") as printer:
            _print_shortcut_values(
                "https://voice.example.ts.net",
                "private-token",
            )
        hidden_output = "\n".join(str(call.args[0]) for call in printer.call_args_list)

        with mock.patch("builtins.print") as printer:
            _print_shortcut_values(
                "https://voice.example.ts.net",
                "private-token",
                reveal_token=True,
            )
        revealed_output = "\n".join(str(call.args[0]) for call in printer.call_args_list)

        self.assertNotIn("private-token", hidden_output)
        self.assertIn("Bearer ***", hidden_output)
        self.assertIn("Bearer private-token", revealed_output)

    def test_app_pairing_endpoint_exchanges_ticket_then_accepts_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = IphoneDeviceRegistry(Path(directory) / "devices.json")
            invitation = registry.create_pairing("Demo iPhone")
            received = []

            def execute(transcript):
                received.append(transcript)
                return {"active_target": "tv"}

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_iphone_handler(
                    registry.authenticate,
                    execute,
                    pairing_redeemer=registry.redeem_pairing,
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                pairing_request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/pairings/redeem",
                    data=json.dumps(
                        {"version": 1, "pairing_code": invitation.code}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(pairing_request, timeout=2) as response:
                    self.assertEqual(response.status, 201)
                    paired = json.loads(response.read().decode())

                self.assertEqual(paired["status"], "paired")
                self.assertEqual(paired["commands_path"], "/v1/commands")
                self.assertNotIn(invitation.code, json.dumps(paired))

                command_request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/commands",
                    data=json.dumps(
                        {
                            "version": 1,
                            "request_id": "5f5e34d8-b878-4925-a976-d4e16fb0a623",
                            "text": "Pause the TV",
                        }
                    ).encode(),
                    headers={
                        "Authorization": f"Bearer {paired['access_token']}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(command_request, timeout=2) as response:
                    command_payload = json.loads(response.read().decode())

                replay_request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/pairings/redeem",
                    data=json.dumps(
                        {"version": 1, "pairing_code": invitation.code}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as replayed:
                    urllib.request.urlopen(replay_request, timeout=2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(received, ["Pause the TV"])
        self.assertEqual(command_payload["status"], "executed")
        self.assertEqual(replayed.exception.code, 400)

    def test_iphone_request_id_prevents_duplicate_execution(self):
        secret = bytes.fromhex("66" * 32)
        calls = []
        request_id = "e8a2a6df-c05c-4afb-b407-a246b79c0ad6"

        def execute(transcript):
            calls.append(transcript)
            return {"active_target": "tv"}

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_iphone_handler(secret, execute, idempotency=IdempotencyStore()),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {"version": 1, "request_id": request_id, "text": "Pause"}
            ).encode()
            payloads = []
            for _ in range(2):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/commands",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {secret.hex()}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    payloads.append(json.loads(response.read().decode()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(calls, ["Pause"])
        self.assertNotIn("replayed", payloads[0])
        self.assertTrue(payloads[1]["replayed"])

    def test_iphone_rate_limit_returns_retry_after(self):
        secret = bytes.fromhex("77" * 32)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_iphone_handler(
                secret,
                lambda _text: {"active_target": "tv"},
                rate_limiter=SlidingWindowRateLimiter(limit=1),
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for index in range(2):
                body = json.dumps(
                    {
                        "version": 1,
                        "request_id": f"00000000-0000-4000-8000-{index:012d}",
                        "text": "Pause",
                    }
                ).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/commands",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {secret.hex()}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                if index == 0:
                    with urllib.request.urlopen(request, timeout=2):
                        pass
                else:
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        urllib.request.urlopen(request, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(rejected.exception.code, 429)
        self.assertGreaterEqual(int(rejected.exception.headers["Retry-After"]), 1)

    def test_signed_action_crosses_real_http_boundary(self):
        secret = bytes.fromhex("22" * 32)
        opened = []

        def opener(url, **kwargs):
            opened.append(url)
            return True

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(secret, opener))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            action = route("Open YouTube on the laptop")
            result = dispatch(
                f"http://127.0.0.1:{server.server_port}", secret, action.to_bytes()
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["status"], "executed")
        self.assertEqual(opened, ["https://www.youtube.com/"])

    def test_signed_latest_episode_action_resolves_and_opens_over_http(self):
        secret = bytes.fromhex("33" * 32)
        opened = []
        media = MediaItem(
            video_id="episode1234",
            title="Newest full episode",
            published_at="2026-08-03T10:00:00Z",
            duration_seconds=2_700,
        )

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                secret,
                lambda url, **kwargs: opened.append(url) is None,
                latest_resolver=lambda: media,
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            action = route("Play the latest Cinkatko podcast")
            result = dispatch(
                f"http://127.0.0.1:{server.server_port}",
                secret,
                action.to_bytes(),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["title"], "Newest full episode")
        self.assertEqual(opened, [media.url])


class YouTubeResolverTests(unittest.TestCase):
    def test_parses_youtube_duration(self):
        self.assertEqual(parse_iso8601_duration("PT1H2M3S"), 3_723)

    def test_video_search_uses_a_bounded_video_query_and_skips_unplayable_items(self):
        calls = []

        def get_json(resource, params):
            calls.append((resource, params))
            return {
                "items": [
                    {"id": {"channelId": "UC" + "z" * 22}},
                    {
                        "id": {"videoId": "upcoming001"},
                        "snippet": {
                            "title": "Upcoming premiere",
                            "liveBroadcastContent": "upcoming",
                        },
                    },
                    {
                        "id": {"videoId": "livevideo01"},
                        "snippet": {
                            "title": "Live stream",
                            "liveBroadcastContent": "live",
                        },
                    },
                    {
                        "id": {"videoId": "video123456"},
                        "snippet": {
                            "title": "The requested video",
                            "publishedAt": "2026-08-12T09:00:00Z",
                            "channelId": "UC" + "a" * 22,
                            "channelTitle": "Example channel",
                            "liveBroadcastContent": "none",
                        },
                    },
                ]
            }

        selected = resolve_video_search(
            "test-key",
            "  lo-fi   hip hop  ",
            get_json=get_json,
        )

        self.assertEqual(selected.video_id, "video123456")
        self.assertEqual(selected.title, "The requested video")
        self.assertEqual(selected.channel_title, "Example channel")
        self.assertEqual(
            calls,
            [
                (
                    "search",
                    {
                        "part": "snippet",
                        "q": "lo-fi hip hop",
                        "type": "video",
                        "order": "relevance",
                        "maxResults": 5,
                        "safeSearch": "moderate",
                        "videoEmbeddable": "true",
                        "key": "test-key",
                    },
                )
            ],
        )

    def test_video_search_rejects_a_response_without_a_playable_video(self):
        def get_json(_resource, _params):
            return {
                "items": [
                    {"id": "not-an-object"},
                    {
                        "id": {"videoId": "upcoming001"},
                        "snippet": {"liveBroadcastContent": "upcoming"},
                    },
                ]
            }

        with self.assertRaisesRegex(
            YouTubeResolverError,
            "No playable YouTube video",
        ):
            resolve_video_search("test-key", "future show", get_json=get_json)

    def test_video_search_rejects_a_malformed_items_collection(self):
        with self.assertRaisesRegex(
            YouTubeResolverError,
            "invalid video search response",
        ):
            resolve_video_search(
                "test-key",
                "elephants",
                get_json=lambda _resource, _params: {"items": None},
            )

    def test_selects_newest_full_episode_and_skips_short(self):
        videos = [
            {
                "id": "short123456",
                "snippet": {
                    "title": "Newest short",
                    "publishedAt": "2026-08-04T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT59S"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
            {
                "id": "episode1234",
                "snippet": {
                    "title": "Newest full episode",
                    "publishedAt": "2026-08-03T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT45M"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
            {
                "id": "episode0000",
                "snippet": {
                    "title": "Older full episode",
                    "publishedAt": "2026-07-20T10:00:00Z",
                    "liveBroadcastContent": "none",
                },
                "contentDetails": {"duration": "PT50M"},
                "status": {"privacyStatus": "public", "embeddable": True},
            },
        ]

        selected = select_latest_episode(videos, min_duration_seconds=600)

        self.assertEqual(selected.video_id, "episode1234")

    def test_resolver_uses_channel_uploads_and_video_details(self):
        calls = []

        def get_json(resource, params):
            calls.append((resource, params))
            if resource == "channels":
                return {
                    "items": [
                        {
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": "UUallowlisted"}
                            }
                        }
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        {"contentDetails": {"videoId": "short123456"}},
                        {"contentDetails": {"videoId": "episode1234"}},
                    ]
                }
            return {
                "items": [
                    {
                        "id": "short123456",
                        "snippet": {
                            "title": "Short",
                            "publishedAt": "2026-08-04T10:00:00Z",
                            "liveBroadcastContent": "none",
                        },
                        "contentDetails": {"duration": "PT45S"},
                        "status": {"privacyStatus": "public", "embeddable": True},
                    },
                    {
                        "id": "episode1234",
                        "snippet": {
                            "title": "Full episode",
                            "publishedAt": "2026-08-03T10:00:00Z",
                            "liveBroadcastContent": "none",
                        },
                        "contentDetails": {"duration": "PT45M"},
                        "status": {"privacyStatus": "public", "embeddable": True},
                    },
                ]
            }

        selected = resolve_latest_episode("test-key", get_json=get_json)

        self.assertEqual(selected.video_id, "episode1234")
        self.assertEqual([resource for resource, _params in calls], [
            "channels",
            "playlistItems",
            "videos",
        ])
        self.assertEqual(calls[0][1]["forHandle"], "@mitoncz")

    def test_resolver_requires_api_key(self):
        with self.assertRaises(YouTubeResolverError):
            resolve_latest_episode("")

    def test_search_resolver_prefers_established_channel_and_filters_shorts(self):
        calls = []

        def get_json(resource, params):
            calls.append((resource, params))
            if resource == "search":
                return {
                    "items": [
                        {
                            "id": {"channelId": "UCaaaaaaaaaaaaaaaaaaaaaa"},
                            "snippet": {"title": "Andrew Huberman"},
                        },
                        {
                            "id": {"channelId": "UCbbbbbbbbbbbbbbbbbbbbbb"},
                            "snippet": {"title": "Huberman Lab Clips"},
                        },
                        {
                            "id": {"channelId": "UCcccccccccccccccccccccc"},
                            "snippet": {"title": "Huberman Lab"},
                        },
                    ]
                }
            if resource == "channels":
                return {
                    "items": [
                        {
                            "id": "UCaaaaaaaaaaaaaaaaaaaaaa",
                            "snippet": {
                                "title": "Andrew Huberman",
                                "description": (
                                    "Andrew Huberman hosts the Huberman Lab Podcast."
                                ),
                            },
                            "statistics": {"subscriberCount": "7610000"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUaaaaaaaaaaaaaaaaaaaaaa"
                                }
                            },
                        },
                        {
                            "id": "UCbbbbbbbbbbbbbbbbbbbbbb",
                            "snippet": {
                                "title": "Huberman Lab Clips",
                                "description": "Clips from Huberman Lab episodes.",
                            },
                            "statistics": {"subscriberCount": "1450000"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUbbbbbbbbbbbbbbbbbbbbbb"
                                }
                            },
                        },
                        {
                            "id": "UCcccccccccccccccccccccc",
                            "snippet": {
                                "title": "Huberman Lab",
                                "description": "Unofficial archive.",
                            },
                            "statistics": {"subscriberCount": "13000"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUcccccccccccccccccccccc"
                                }
                            },
                        }
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        {"contentDetails": {"videoId": "short123456"}},
                        {"contentDetails": {"videoId": "episode1234"}},
                    ]
                }
            return {
                "items": [
                    {
                        "id": "short123456",
                        "snippet": {
                            "title": "A short clip",
                            "publishedAt": "2026-08-05T10:00:00Z",
                            "liveBroadcastContent": "none",
                        },
                        "contentDetails": {"duration": "PT45S"},
                        "status": {"privacyStatus": "public", "embeddable": True},
                    },
                    {
                        "id": "episode1234",
                        "snippet": {
                            "title": "Full Huberman Lab episode",
                            "publishedAt": "2026-08-04T10:00:00Z",
                            "liveBroadcastContent": "none",
                        },
                        "contentDetails": {"duration": "PT2H"},
                        "status": {"privacyStatus": "public", "embeddable": True},
                    },
                ]
            }

        selected = resolve_latest_search(
            "test-key",
            "  Huberman   Lab  ",
            get_json=get_json,
        )

        self.assertEqual(selected.video_id, "episode1234")
        self.assertEqual(
            [resource for resource, _params in calls],
            ["search", "channels", "playlistItems", "videos"],
        )
        self.assertEqual(calls[0][1]["q"], "Huberman Lab podcast")
        self.assertEqual(calls[0][1]["type"], "channel")
        self.assertEqual(calls[0][1]["order"], "relevance")
        self.assertEqual(
            calls[1][1]["id"],
            "UCaaaaaaaaaaaaaaaaaaaaaa,UCbbbbbbbbbbbbbbbbbbbbbb,"
            "UCcccccccccccccccccccccc",
        )
        self.assertEqual(
            calls[2][1]["playlistId"], "UUaaaaaaaaaaaaaaaaaaaaaa"
        )

    def test_search_resolver_connects_a_host_name_to_their_podcast_channel(self):
        calls = []

        def get_json(resource, params):
            calls.append((resource, params))
            if resource == "search":
                return {
                    "items": [
                        {
                            "id": {"channelId": "UCaaaaaaaaaaaaaaaaaaaaaa"},
                            "snippet": {"title": "Casey Newton"},
                        },
                        {
                            "id": {"channelId": "UCbbbbbbbbbbbbbbbbbbbbbb"},
                            "snippet": {"title": "Hard Fork"},
                        },
                        {
                            "id": {"channelId": "UCcccccccccccccccccccccc"},
                            "snippet": {"title": "Huge Unrelated Podcast"},
                        },
                    ]
                }
            if resource == "channels":
                return {
                    "items": [
                        {
                            "id": "UCaaaaaaaaaaaaaaaaaaaaaa",
                            "snippet": {
                                "title": "Casey Newton",
                                "description": (
                                    "Email salesman and podcast co-host at Hard Fork."
                                ),
                            },
                            "statistics": {"subscriberCount": "1400"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUaaaaaaaaaaaaaaaaaaaaaa"
                                }
                            },
                        },
                        {
                            "id": "UCbbbbbbbbbbbbbbbbbbbbbb",
                            "snippet": {
                                "title": "Hard Fork",
                                "description": (
                                    "Hosts Kevin Roose and Casey Newton explore tech. "
                                    "Weekly on Fridays."
                                ),
                            },
                            "statistics": {"subscriberCount": "63000"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUbbbbbbbbbbbbbbbbbbbbbb"
                                }
                            },
                        },
                        {
                            "id": "UCcccccccccccccccccccccc",
                            "snippet": {
                                "title": "Huge Unrelated Podcast",
                                "description": "Celebrity interviews.",
                            },
                            "statistics": {"subscriberCount": "9000000"},
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUcccccccccccccccccccccc"
                                }
                            },
                        },
                    ]
                }
            if resource == "playlistItems":
                self.assertEqual(
                    params["playlistId"], "UUbbbbbbbbbbbbbbbbbbbbbb"
                )
                return {
                    "items": [
                        {"contentDetails": {"videoId": "episode1234"}}
                    ]
                }
            return {
                "items": [
                    {
                        "id": "episode1234",
                        "snippet": {
                            "title": "Newest Hard Fork episode",
                            "publishedAt": "2026-08-05T10:00:00Z",
                            "liveBroadcastContent": "none",
                            "channelId": "UCbbbbbbbbbbbbbbbbbbbbbb",
                            "channelTitle": "Hard Fork",
                        },
                        "contentDetails": {"duration": "PT45M"},
                        "status": {
                            "privacyStatus": "public",
                            "embeddable": True,
                        },
                    }
                ]
            }

        selected = resolve_latest_search(
            "test-key",
            "Casey Newton",
            get_json=get_json,
            catalog=PodcastCatalog.empty(),
        )

        self.assertEqual(calls[0][1]["q"], "Casey Newton podcast")
        self.assertEqual(selected.title, "Newest Hard Fork episode")
        self.assertEqual(selected.channel_title, "Hard Fork")
        self.assertEqual(selected.channel_id, "UCbbbbbbbbbbbbbbbbbbbbbb")

    def test_search_resolver_rejects_popular_but_textually_unrelated_channel(self):
        def get_json(resource, _params):
            if resource == "search":
                return {
                    "items": [
                        {
                            "id": {"channelId": "UCaaaaaaaaaaaaaaaaaaaaaa"},
                            "snippet": {"title": "Huge Unrelated Podcast"},
                        }
                    ]
                }
            return {
                "items": [
                    {
                        "id": "UCaaaaaaaaaaaaaaaaaaaaaa",
                        "snippet": {
                            "title": "Huge Unrelated Podcast",
                            "description": "Celebrity interviews.",
                        },
                        "statistics": {"subscriberCount": "9000000"},
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": "UUunrelated"}
                        },
                    }
                ]
            }

        with self.assertRaisesRegex(
            YouTubeResolverError,
            "sufficiently relevant",
        ):
            resolve_latest_search(
                "test-key",
                "Casey Newton",
                get_json=get_json,
                catalog=PodcastCatalog.empty(),
            )

    def test_catalog_marks_host_only_request_as_ambiguous(self):
        calls = []

        with self.assertRaisesRegex(
            YouTubeResolverError,
            "Hard Fork, Platformer",
        ):
            resolve_latest_search(
                "test-key",
                "Casey Newton podcast",
                get_json=lambda resource, params: calls.append((resource, params)),
            )

        self.assertEqual(calls, [])

    def test_catalog_resolves_platformer_to_verified_casey_newton_channel(self):
        calls = []

        def get_json(resource, params):
            calls.append((resource, params))
            if resource == "channels":
                return {
                    "items": [
                        {
                            "id": "UCaKZIS0BjZffyCfayy3ciCA",
                            "contentDetails": {
                                "relatedPlaylists": {
                                    "uploads": "UUaKZIS0BjZffyCfayy3ciCA"
                                }
                            },
                            "snippet": {"title": "Casey Newton"},
                        }
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        {"contentDetails": {"videoId": "episode1234"}}
                    ]
                }
            return {
                "items": [
                    {
                        "id": "episode1234",
                        "snippet": {
                            "title": "Newest Platformer episode",
                            "publishedAt": "2026-08-05T10:00:00Z",
                            "liveBroadcastContent": "none",
                            "channelId": "UCaKZIS0BjZffyCfayy3ciCA",
                            "channelTitle": "Casey Newton",
                        },
                        "contentDetails": {"duration": "PT50M"},
                        "status": {
                            "privacyStatus": "public",
                            "embeddable": True,
                        },
                    }
                ]
            }

        selected = resolve_latest_search(
            "test-key",
            "Platformer podcast",
            get_json=get_json,
        )

        self.assertEqual(calls[0][0], "channels")
        self.assertEqual(
            calls[0][1]["id"],
            "UCaKZIS0BjZffyCfayy3ciCA",
        )
        self.assertEqual(selected.channel_title, "Casey Newton")
        self.assertEqual(selected.title, "Newest Platformer episode")

    def test_default_catalog_contains_both_casey_newton_podcasts(self):
        catalog = load_podcast_catalog()

        self.assertEqual(catalog.lookup("Hard Fork podcast").title, "Hard Fork")
        self.assertEqual(catalog.lookup("Platformer").title, "Platformer")
        self.assertEqual(
            [source.title for source in catalog.choices("Casey Newton")],
            ["Hard Fork", "Platformer"],
        )

    def test_search_resolver_rejects_empty_query(self):
        with self.assertRaises(YouTubeResolverError):
            resolve_latest_search("test-key", "   ")


class StateMachineTests(unittest.TestCase):
    def test_voice_command_happy_path(self):
        events = []
        state = StateMachine(observers=(events.append,), clock=lambda: 123.0)

        for next_state in (
            AgentState.READY,
            AgentState.LISTENING,
            AgentState.TRANSCRIBING,
            AgentState.ROUTING,
            AgentState.EXECUTING,
            AgentState.SUCCESS,
        ):
            state.transition(next_state)

        self.assertEqual(state.state, AgentState.SUCCESS)
        self.assertEqual([event.sequence for event in events], list(range(1, 7)))
        self.assertEqual(events[0].previous, AgentState.STARTING)
        self.assertEqual(events[-1].occurred_at, 123.0)

    def test_text_mode_can_skip_audio_states(self):
        state = StateMachine()
        state.transition(AgentState.READY)
        state.transition(AgentState.ROUTING)
        self.assertEqual(state.state, AgentState.ROUTING)

    def test_success_can_rearm_for_a_button_command(self):
        state = StateMachine()
        state.transition(AgentState.READY)
        state.transition(AgentState.ROUTING)
        state.transition(AgentState.EXECUTING)
        state.transition(AgentState.SUCCESS)
        state.transition(AgentState.READY)
        self.assertEqual(state.state, AgentState.READY)

    def test_error_can_rearm_for_a_button_command(self):
        state = StateMachine()
        state.transition(AgentState.READY)
        state.transition(AgentState.ERROR)
        state.transition(AgentState.READY)
        self.assertEqual(state.state, AgentState.READY)

    def test_invalid_transition_is_rejected(self):
        state = StateMachine()
        with self.assertRaises(InvalidStateTransition):
            state.transition(AgentState.SUCCESS)
        self.assertEqual(state.state, AgentState.STARTING)

    def test_observer_failure_does_not_break_command_state(self):
        received = []
        observer_errors = []

        def broken_observer(event):
            raise RuntimeError(f"cannot render {event.state.value}")

        state = StateMachine(
            observers=(broken_observer, received.append),
            on_observer_error=lambda observer, exc: observer_errors.append(str(exc)),
        )
        event = state.transition(AgentState.READY)

        self.assertEqual(state.state, AgentState.READY)
        self.assertEqual(received, [event])
        self.assertEqual(observer_errors, ["cannot render READY"])


class RoomCanvasTests(unittest.TestCase):
    def test_canvas_tracks_command_content_and_stage_timings(self):
        now = {"value": 100.0}
        canvas = RoomCanvasState(clock=lambda: now["value"])
        state = StateMachine(observers=(canvas.observe_state,), clock=lambda: now["value"])

        state.transition(AgentState.READY)
        canvas.start_command()
        now["value"] = 101.0
        state.transition(AgentState.LISTENING, "waiting for one utterance")
        now["value"] = 102.25
        state.transition(AgentState.TRANSCRIBING, "language=en")
        canvas.record_transcript("Play the latest Cinkatko podcast")
        now["value"] = 104.0
        state.transition(AgentState.ROUTING)
        action = ActionRequest.create(
            "youtube.play_latest",
            "tv",
            {"channel": "cinkatko", "selector": "latest_episode"},
        )
        canvas.record_action(action)
        now["value"] = 104.2
        state.transition(AgentState.EXECUTING, "youtube.play_latest -> tv")
        canvas.record_result(
            {"status": "executed", "title": "Newest episode", "request_id": "hidden"}
        )
        now["value"] = 105.5
        state.transition(AgentState.SUCCESS, "executed")

        snapshot = canvas.snapshot()
        self.assertEqual(snapshot["transcript"], "Play the latest Cinkatko podcast")
        self.assertEqual(snapshot["action"], "youtube.play_latest")
        self.assertEqual(snapshot["target"], "tv")
        self.assertEqual(snapshot["result"]["title"], "Newest episode")
        self.assertNotIn("request_id", snapshot["result"])
        self.assertEqual(snapshot["stage_ms"]["listening"], 1250)
        self.assertEqual(snapshot["stage_ms"]["transcribing"], 1750)
        self.assertEqual(snapshot["stage_ms"]["routing"], 200)
        self.assertEqual(snapshot["stage_ms"]["executing"], 1300)
        self.assertEqual(snapshot["total_ms"], 5500)

    def test_canvas_server_is_loopback_and_serves_live_state(self):
        canvas = RoomCanvasServer(port=0)
        url = canvas.start()
        try:
            canvas.state.start_command()
            canvas.state.record_transcript("Open YouTube")
            with urllib.request.urlopen(url + "api/state", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(url, timeout=2) as response:
                page = response.read().decode("utf-8")
        finally:
            canvas.stop()

        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertEqual(payload["transcript"], "Open YouTube")
        self.assertIn("Room Agent", page)

    def test_canvas_server_does_not_expose_unknown_files(self):
        canvas = RoomCanvasServer(port=0)
        url = canvas.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(url + "../pairing.key", timeout=2)
        finally:
            canvas.stop()

        self.assertEqual(raised.exception.code, 404)


class FakeUsbDevice:
    def __init__(self):
        self.calls = []
        self.failure = None

    def ctrl_transfer(self, *args):
        if self.failure is not None:
            raise self.failure
        self.calls.append(args)


class LedObserverTests(unittest.TestCase):
    def test_ready_state_sets_brightness_and_cyan(self):
        device = FakeUsbDevice()
        led = ReSpeakerLedObserver(device=device, brightness=9)
        led(
            StateEvent(
                sequence=1,
                previous=AgentState.STARTING,
                state=AgentState.READY,
                occurred_at=1.0,
            )
        )

        self.assertEqual(device.calls[0][2], COMMAND_BRIGHTNESS)
        self.assertEqual(device.calls[0][4], [9])
        self.assertEqual(device.calls[1][2], COMMAND_MONO_COLOR)
        self.assertEqual(device.calls[1][4], [0, 24, 24, 0])

    def test_stopped_state_turns_leds_off(self):
        device = FakeUsbDevice()
        led = ReSpeakerLedObserver(device=device)
        led(
            StateEvent(
                sequence=1,
                previous=AgentState.READY,
                state=AgentState.STOPPED,
                occurred_at=1.0,
            )
        )
        self.assertEqual(device.calls[-1][4], [0, 0, 0, 0])

    def test_listening_uses_firmware_voice_direction_pattern(self):
        device = FakeUsbDevice()
        led = ReSpeakerLedObserver(device=device)
        led(
            StateEvent(
                sequence=1,
                previous=AgentState.READY,
                state=AgentState.LISTENING,
                occurred_at=1.0,
            )
        )

        self.assertEqual(device.calls[-1][2], COMMAND_LISTEN)
        self.assertEqual(device.calls[-1][4], [0])

    def test_custom_direction_uses_nearest_pixel_and_neighbors(self):
        device = FakeUsbDevice()
        led = ReSpeakerLedObserver(device=device)

        pixel = led.show_direction(31)

        self.assertEqual(pixel, 1)
        self.assertEqual(device.calls[-1][2], COMMAND_CUSTOM)
        data = device.calls[-1][4]
        self.assertEqual(len(data), 48)
        self.assertEqual(data[4:8], [0, 128, 255, 0])
        self.assertEqual(data[0:4], [0, 48, 96, 0])
        self.assertEqual(data[8:12], [0, 48, 96, 0])

    def test_direction_offset_rotates_pixel_mapping(self):
        self.assertEqual(ReSpeakerLedObserver.pixel_for_direction(0), 0)
        self.assertEqual(
            ReSpeakerLedObserver.pixel_for_direction(0, offset=90), 3
        )
        self.assertEqual(ReSpeakerLedObserver.pixel_for_direction(359), 0)

    def test_usb_failure_disables_observer_after_first_error(self):
        device = FakeUsbDevice()
        led = ReSpeakerLedObserver(device=device)
        device.failure = OSError("USB disconnected")
        event = StateEvent(
            sequence=1,
            previous=AgentState.STARTING,
            state=AgentState.READY,
            occurred_at=1.0,
        )

        with self.assertRaises(OSError):
            led(event)
        self.assertFalse(led.enabled)
        led(event)


class FakeTuningUsbDevice:
    def __init__(self):
        self.calls = []
        self.values = {(0xC0, 21): 47, (0xE0, 19): 1}

    def ctrl_transfer(self, *args):
        self.calls.append(args)
        value = self.values[(args[2], args[3])]
        return struct.pack("<ii", value, 359)


class TuningTests(unittest.TestCase):
    def test_reads_direction_and_voice_activity_from_documented_parameters(self):
        device = FakeTuningUsbDevice()
        tuning = ReSpeakerTuning(device=device)

        self.assertEqual(tuning.direction, 47)
        self.assertTrue(tuning.voice_activity)
        self.assertEqual(device.calls[0][0:5], (0xC0, 0, 0xC0, 21, 8))
        self.assertEqual(device.calls[1][0:5], (0xC0, 0, 0xE0, 19, 8))

    def test_rejects_invalid_direction(self):
        device = FakeTuningUsbDevice()
        device.values[(0xC0, 21)] = 360

        with self.assertRaises(TuningUnavailable):
            _ = ReSpeakerTuning(device=device).direction

    def test_rejects_short_usb_response(self):
        class ShortResponseDevice:
            def ctrl_transfer(self, *args):
                return b"short"

        with self.assertRaises(TuningUnavailable):
            ReSpeakerTuning(device=ShortResponseDevice()).read_int(21, 0)

if __name__ == "__main__":
    unittest.main()
