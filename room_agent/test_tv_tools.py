from __future__ import annotations

import tempfile
import unittest
import importlib.util
import os
import stat
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from configure_iphone import _print_shortcut_values
from protocol import (
    ProtocolError,
    _apply_windows_private_acl,
    generate_key,
    read_key,
    write_private_text,
)
from setup_tv_connector import (
    _choose_profile,
    connect_host,
    inspect_configuration,
    main as setup_main,
    prepare_connector,
    run_platformio,
)
from tv_connector import ConnectorPlan, ConnectorStep, TvConnectorError
from tv_remote import build_parser as build_remote_parser, dispatch, resolve_connector_url


class FirmwareSecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parent
            / "firmware"
            / "atoms3u_tv_connector"
            / "src"
            / "main.cpp"
        ).read_text(encoding="utf-8")
        cls.response_auth_contract = (
            Path(__file__).resolve().parent
            / "firmware"
            / "atoms3u_tv_connector"
            / "include"
            / "response_auth_contract.h"
        ).read_text(encoding="utf-8")

    def test_execution_responses_are_authenticated(self):
        self.assertIn("kResponseSignatureHeader", self.source)
        self.assertIn("hmacResponseBody(body)", self.source)
        self.assertIn("sendJson(client, 200, response, true)", self.source)
        self.assertIn(
            "X-Room-Agent-Response-Signature", self.response_auth_contract
        )
        self.assertIn("room-agent-response-v1\\n", self.response_auth_contract)

    def test_replay_cache_does_not_evict_live_nonces(self):
        self.assertIn('#include "replay_nonce_cache.h"', self.source)
        self.assertIn("ReplayNonceResult::Full", self.source)
        self.assertNotIn("nextNonce", self.source)

    def test_execution_waits_for_trusted_network_time(self):
        self.assertIn('error = "clock_not_synchronized"', self.source)
        self.assertNotIn("settimeofday", self.source)

    def test_execution_is_bound_to_the_current_boot_identifier(self):
        self.assertIn('document["boot_id"] = bootId', self.source)
        self.assertIn(
            "suppliedBootId.c_str(), suppliedBootId.size(), bootId",
            self.source,
        )
        self.assertLess(
            self.source.index("contractBootIdMatches"),
            self.source.index("consumeVerifiedNonce(verified"),
        )

    def test_execute_body_is_bounded_before_network_read(self):
        self.assertIn("WiFiServer server(80)", self.source)
        self.assertIn("readBoundedHttpLine", self.source)
        self.assertIn("readBoundedHttpBody", self.source)
        self.assertIn("parseContractContentLength", self.source)
        self.assertNotIn("WebServer", self.source)
        self.assertLess(
            self.source.index("parsed > kMaxBodyBytes"),
            self.source.index(
                "readBoundedHttpBody(",
                self.source.index("parsed > kMaxBodyBytes"),
            ),
        )
        self.assertEqual(
            self.source.count("const uint32_t requestStarted = millis();"),
            1,
        )
        self.assertNotIn("const uint32_t started = millis();", self.source)
        self.assertIn("validContractHttpLineOctet(next)", self.source)
        self.assertIn("output[length - 1] != static_cast<char>(13)", self.source)

    def test_firmware_uses_explicit_json_string_lengths(self):
        for field in ("request_id", "boot_id", "profile", "purpose"):
            self.assertIn(f'document["{field}"].as<JsonString>()', self.source)
        self.assertIn('step["op"].as<JsonString>()', self.source)
        self.assertIn('step["key"].as<JsonString>()', self.source)
        self.assertIn('step["text"].as<JsonString>()', self.source)
        self.assertIn("validContractPrintableAscii", self.source)

    def test_boot_identifier_is_generated_after_wifi_entropy_is_enabled(self):
        setup_source = self.source[self.source.index("void setup()") :]
        self.assertLess(setup_source.index("WiFi.begin("), setup_source.index("esp_fill_random("))
        self.assertLess(setup_source.index("esp_fill_random("), setup_source.index("void loop()"))

    def test_host_requires_the_same_canonical_request_id_as_firmware(self):
        plan = ConnectorPlan(
            request_id="8dc4207a65b94606b5afa1f5613f8957",
            boot_id="11" * 16,
            profile="generic-usb-hid-v1",
            purpose="media.pause",
            steps=(ConnectorStep.key("PLAY_PAUSE"),),
        )

        with self.assertRaisesRegex(TvConnectorError, "canonical UUID"):
            plan.validate()


class SetupScriptTests(unittest.TestCase):
    def test_python_version_gate_requires_3_11_or_newer(self):
        checker_path = Path(__file__).resolve().parent.parent / "scripts" / "check_python.py"
        self.assertTrue(checker_path.is_file())
        spec = importlib.util.spec_from_file_location("check_python", checker_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertFalse(module.is_supported((3, 10, 14)))
        self.assertTrue(module.is_supported((3, 11, 0)))
        self.assertTrue(module.is_supported((3, 14, 0)))

        def incompatible_import(module_name):
            if module_name == "winrt.runtime":
                raise ImportError("wrong interpreter ABI")
            return object()

        self.assertEqual(
            module.installed_environment_errors(
                platform_name="nt",
                importer=incompatible_import,
            ),
            ["winrt.runtime: wrong interpreter ABI"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX mode contract")
    @mock.patch("os.fchmod", side_effect=OSError("permission denied"))
    def test_key_generation_fails_closed_when_private_permissions_fail(self, _chmod):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "connector.key"

            with self.assertRaisesRegex(ProtocolError, "private permissions"):
                generate_key(key_file)

            self.assertFalse(key_file.exists())

    @unittest.skipUnless(os.name == "nt", "Windows ACL contract")
    @mock.patch(
        "protocol._apply_windows_private_acl",
        side_effect=OSError("permission denied"),
    )
    def test_key_generation_fails_closed_when_windows_acl_fails(self, _acl):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "connector.key"

            with self.assertRaisesRegex(ProtocolError, "private permissions"):
                generate_key(key_file)

            self.assertFalse(key_file.exists())

    @mock.patch(
        "protocol._restrict_private_permissions",
        side_effect=subprocess.TimeoutExpired("icacls", 10),
    )
    def test_key_generation_cleans_up_after_permission_timeout(self, _permissions):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "connector.key"

            with self.assertRaisesRegex(ProtocolError, "private permissions"):
                generate_key(key_file)

            self.assertFalse(key_file.exists())

    @mock.patch(
        "protocol.subprocess.run",
        side_effect=subprocess.TimeoutExpired("whoami", 10),
    )
    def test_windows_acl_timeout_is_normalized(self, _run):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "owner-only Windows ACL"):
                _apply_windows_private_acl(Path(directory) / "connector.key")

    def test_windows_acl_subprocess_failure_exposes_only_exception_class(self):
        private_path = Path(r"C:\private\connector.key")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=5,
            stdout="System.UnauthorizedAccessException\n",
            stderr=f"secret detail at {private_path}",
        )
        with mock.patch("protocol.subprocess.run", return_value=completed):
            with self.assertRaises(OSError) as raised:
                _apply_windows_private_acl(private_path)

        message = str(raised.exception)
        self.assertIn("System.UnauthorizedAccessException", message)
        self.assertNotIn(str(private_path), message)
        self.assertNotIn(completed.stderr, message)

    def test_windows_acl_subprocess_failure_rejects_oversized_exception_class(self):
        diagnostic = "System." + ("A" * 200) + "Exception"
        completed = subprocess.CompletedProcess(
            args=[], returncode=5, stdout=diagnostic, stderr="secret detail"
        )
        with mock.patch("protocol.subprocess.run", return_value=completed):
            with self.assertRaises(OSError) as raised:
                _apply_windows_private_acl(Path("connector.key"))

        self.assertNotIn(diagnostic, str(raised.exception))

    def test_windows_acl_application_builds_one_owner_only_acl(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"runner","S-1-5-21-1000"\n', stderr=""
        )
        with mock.patch("protocol.subprocess.run", return_value=completed) as run:
            _apply_windows_private_acl(Path("connector.key"))

        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("powershell.exe", command[0].lower())
        script = command[command.index("-Command") + 1]
        self.assertIn("$acl.SetOwner($current)", script)
        self.assertIn("$acl.SetAccessRuleProtection($true,$false)", script)
        self.assertIn("$acl.AddAccessRule($rule)", script)
        self.assertIn("[System.IO.File]::SetAccessControl($TargetPath,$acl)", script)
        self.assertNotIn("Set-Acl", script)
        self.assertNotIn("connector.key", command)
        self.assertEqual(
            run.call_args.kwargs["env"]["ROOM_AGENT_PRIVATE_PATH"], "connector.key"
        )

    def test_windows_acl_contract_requires_the_current_user_as_owner(self):
        source = (Path(__file__).resolve().parent / "protocol.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("$owner=$acl.GetOwner", source)
        self.assertIn("$owner -ne $current", source)
        self.assertIn("$rule.IsInherited", source)
        self.assertIn("$rule.FileSystemRights -ne $full", source)
        self.assertIn("[System.IO.File]::GetAccessControl($TargetPath)", source)
        self.assertNotIn("$acl=Get-Acl", source)

    @unittest.skipUnless(os.name == "nt", "Windows ACL contract")
    def test_windows_acl_handles_literal_special_character_path(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "connector ; quote ' marker.key"

            generate_key(key_file)

            self.assertEqual(len(read_key(key_file)), 32)

    @unittest.skipUnless(os.name == "nt", "Windows ACL contract")
    def test_key_generation_removes_inherited_windows_access(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "connector.key"

            generate_key(key_file)

            result = subprocess.run(
                [
                    str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
                    str(key_file),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertNotIn("(I)", result.stdout)


class TvSetupTests(unittest.TestCase):
    def test_example_environment_keeps_optional_connector_disabled(self):
        example = (Path(__file__).resolve().parent / ".env.example").read_text(
            encoding="utf-8"
        )

        values = dict(
            line.split("=", 1)
            for line in example.splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        self.assertEqual(values["TV_CONNECTOR_URL"], "")
        self.assertEqual(values["TV_CONNECTOR_BLE_NAME"], "")
        self.assertEqual(values["TV_CONNECTOR_KEY_FILE"], "")

    @mock.patch("builtins.input", side_effect=["0", "1"])
    @mock.patch("builtins.print")
    def test_profile_menu_rejects_zero_instead_of_selecting_the_last_profile(
        self, _print, _input
    ):
        self.assertEqual(_choose_profile(), "generic-usb-hid-v1")

    def test_prepare_creates_private_files_and_reuses_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            template.write_text(
                '#pragma once\n'
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX \\\n'
                f'  "{"0" * 64}"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                encoding="utf-8",
            )
            key_file = root / "tv-connector.key"
            secrets_file = root / "secrets.h"

            created = prepare_connector(
                profile="lg-tv-ir-v1",
                ssid="Home WiFi",
                password="correct-horse",
                key_file=key_file,
                secrets_file=secrets_file,
                template_file=template,
            )
            original_key = read_key(key_file)
            reused = prepare_connector(
                profile="sony-bravia-ir-v1",
                ssid='New "WiFi"',
                password=r"battery\staple",
                key_file=key_file,
                secrets_file=secrets_file,
                template_file=template,
            )

            self.assertEqual(created, (True, True))
            self.assertEqual(reused, (False, False))
            self.assertEqual(read_key(key_file), original_key)
            content = secrets_file.read_text(encoding="utf-8")
            self.assertIn(r'ROOM_AGENT_WIFI_SSID "New \"WiFi\""', content)
            self.assertIn(r'ROOM_AGENT_WIFI_PASSWORD "battery\\staple"', content)
            self.assertIn('ROOM_AGENT_CONNECTOR_PROFILE "sony-bravia-ir-v1"', content)
            self.assertNotIn("correct-horse", content)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(secrets_file.stat().st_mode), 0o600)

    def test_prepare_migrates_an_existing_permissive_key_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            template.write_text(
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                encoding="utf-8",
            )
            key_file = root / "tv-connector.key"
            key_file.write_text("ab" * 32, encoding="ascii")
            if os.name == "posix":
                key_file.chmod(0o644)

            created = prepare_connector(
                profile="lg-tv-ir-v1",
                ssid="Home WiFi",
                password="safe-password",
                key_file=key_file,
                secrets_file=root / "secrets.h",
                template_file=template,
            )

            self.assertEqual(created, (False, True))
            self.assertEqual(read_key(key_file), bytes.fromhex("ab" * 32))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)

    def test_prepare_rejects_a_nonregular_existing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            template.write_text(
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                encoding="utf-8",
            )
            key_path = root / "tv-connector.key"
            key_path.mkdir()

            with self.assertRaisesRegex(ProtocolError, "pairing key|regular file"):
                prepare_connector(
                    profile="lg-tv-ir-v1",
                    ssid="Home WiFi",
                    password="safe-password",
                    key_file=key_path,
                    secrets_file=root / "secrets.h",
                    template_file=template,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_prepare_rejects_a_symlinked_existing_secrets_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            settings = (
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n'
            )
            template.write_text(settings, encoding="utf-8")
            key_file = root / "tv-connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            target = root / "target-secrets.h"
            target.write_text(settings, encoding="utf-8")
            secrets_file = root / "secrets.h"
            secrets_file.symlink_to(target)

            with self.assertRaisesRegex(ProtocolError, "stable|symbolic|settings"):
                prepare_connector(
                    profile="lg-tv-ir-v1",
                    ssid="Home WiFi",
                    password="safe-password",
                    key_file=key_file,
                    secrets_file=secrets_file,
                    template_file=template,
                )

    @mock.patch("setup_tv_connector._platformio_available", return_value=True)
    def test_doctor_rejects_permissive_existing_private_files(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "tv-connector.key"
            secrets_file = root / "secrets.h"
            key_file.write_text("ab" * 32, encoding="ascii")
            secrets_file.write_text(
                '#define ROOM_AGENT_WIFI_SSID "TV-Network"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "safe-password"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "ab" * 32 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "lg-tv-ir-v1"\n',
                encoding="utf-8",
            )
            if os.name == "posix":
                key_file.chmod(0o644)
                secrets_file.chmod(0o644)

            checks = inspect_configuration(
                key_file=key_file,
                secrets_file=secrets_file,
            )

            self.assertFalse(checks[0][0])
            self.assertFalse(checks[1][0])

    @mock.patch("pathlib.Path.replace", side_effect=OSError("simulated write failure"))
    def test_prepare_rolls_back_new_private_files_when_final_write_fails(self, _replace):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            template.write_text(
                '#pragma once\n'
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                encoding="utf-8",
            )
            key_file = root / "tv-connector.key"
            secrets_file = root / "secrets.h"

            with self.assertRaisesRegex(ProtocolError, "update private connector settings"):
                prepare_connector(
                    profile="lg-tv-ir-v1",
                    ssid="Home WiFi",
                    password="correct-horse",
                    key_file=key_file,
                    secrets_file=secrets_file,
                    template_file=template,
                )

            self.assertFalse(key_file.exists())
            self.assertFalse(secrets_file.exists())

    @mock.patch("setup_tv_connector._platformio_available", return_value=True)
    def test_doctor_reports_ready_without_exposing_secrets(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "secrets.example.h"
            template.write_text(
                '#define ROOM_AGENT_WIFI_SSID "your-2.4-ghz-wifi"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "replace-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "0" * 64 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "generic-usb-hid-v1"\n',
                encoding="utf-8",
            )
            key_file = root / "tv-connector.key"
            secrets_file = root / "secrets.h"
            prepare_connector(
                profile="generic-usb-hid-v1",
                ssid="Private Network",
                password="not-printed",
                key_file=key_file,
                secrets_file=secrets_file,
                template_file=template,
            )

            checks = inspect_configuration(key_file=key_file, secrets_file=secrets_file)

            self.assertTrue(all(passed for passed, _message in checks))
            self.assertNotIn("Private Network", repr(checks))
            self.assertNotIn("not-printed", repr(checks))
            self.assertNotIn(key_file.read_text(encoding="ascii"), repr(checks))

    def test_doctor_reports_corrupt_private_files_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "tv-connector.key"
            secrets_file = root / "secrets.h"
            key_file.write_bytes(b"\xff\xfe")
            secrets_file.write_bytes(b"\xff\xfe")

            checks = inspect_configuration(
                key_file=key_file,
                secrets_file=secrets_file,
            )

            self.assertFalse(checks[0][0])
            self.assertFalse(checks[1][0])

    @mock.patch("setup_tv_connector._platformio_available", return_value=True)
    def test_doctor_rejects_documented_password_placeholders(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "tv-connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            secrets_file = root / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_WIFI_SSID "TV-Network"\n'
                '#define ROOM_AGENT_WIFI_PASSWORD "change-me"\n'
                '#define ROOM_AGENT_CONNECTOR_KEY_HEX "' + "ab" * 32 + '"\n'
                '#define ROOM_AGENT_CONNECTOR_PROFILE "lg-tv-ir-v1"\n',
                description="test firmware settings",
            )

            checks = inspect_configuration(key_file=key_file, secrets_file=secrets_file)

            password_check = next(
                passed for passed, message in checks if message == "Wi-Fi password is configured"
            )
            self.assertFalse(password_check)

    @mock.patch("setup_tv_connector.subprocess.run")
    @mock.patch("setup_tv_connector._platformio_command", return_value=["python", "-m", "platformio"])
    def test_flash_selects_the_u002_environment_and_optional_port(self, _command, run):
        run.return_value.returncode = 0

        result = run_platformio(emitter="u002", upload=True, port="COM7")

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        clean_command = run.call_args_list[0].args[0]
        command = run.call_args_list[1].args[0]
        self.assertEqual(clean_command[-2:], ["-t", "clean"])
        self.assertIn("atoms3u-tv-connector-u002", command)
        self.assertIn("upload", command)
        self.assertEqual(command[-2:], ["--upload-port", "COM7"])
        self.assertFalse(run.call_args_list[1].kwargs["check"])

    @mock.patch("setup_tv_connector.subprocess.run")
    @mock.patch("setup_tv_connector._platformio_command", return_value=["pio"])
    def test_build_stops_when_clean_fails(self, _command, run):
        run.return_value.returncode = 7

        result = run_platformio(emitter="built-in", upload=False)

        self.assertEqual(result, 7)
        self.assertEqual(run.call_count, 1)

    def test_nonexistent_emitter_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "Emitter"):
            run_platformio(emitter="both", upload=False)

    @mock.patch("setup_tv_connector.configure_wifi_host")
    @mock.patch("setup_tv_connector.UsbTvConnectorClient")
    def test_connect_host_verifies_profile_before_saving(self, client_type, configure_host):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "tv-connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            secrets_file = root / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_CONNECTOR_PROFILE "lg-tv-ir-v1"\n',
                description="test firmware settings",
            )
            env_file = root / ".env"
            client = client_type.return_value
            client.base_url = "http://192.168.1.50:80"
            client.health.return_value = {"profile": "lg-tv-ir-v1"}

            result = connect_host(
                url="http://192.168.1.50",
                key_file=key_file,
                secrets_file=secrets_file,
                env_file=env_file,
            )

            self.assertEqual(result, ("http://192.168.1.50:80", "lg-tv-ir-v1"))
            client.verify.assert_called_once_with()
            configure_host.assert_called_once_with(
                env_file,
                key_file,
                "http://192.168.1.50",
                "lg-tv-ir-v1",
            )

    @mock.patch("setup_tv_connector.configure_wifi_host")
    @mock.patch("setup_tv_connector.UsbTvConnectorClient")
    def test_connect_host_rejects_profile_mismatch(self, client_type, configure_host):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "tv-connector.key"
            write_private_text(key_file, "ab" * 32, description="test connector key")
            secrets_file = root / "secrets.h"
            write_private_text(
                secrets_file,
                '#define ROOM_AGENT_CONNECTOR_PROFILE "lg-tv-ir-v1"\n',
                description="test firmware settings",
            )
            client_type.return_value.health.return_value = {
                "profile": "generic-usb-hid-v1"
            }

            with self.assertRaisesRegex(ProtocolError, "does not match"):
                connect_host(
                    url="http://192.168.1.50",
                    key_file=key_file,
                    secrets_file=secrets_file,
                    env_file=root / ".env",
                )

            configure_host.assert_not_called()

    @mock.patch("setup_tv_connector.connect_host", side_effect=TvConnectorError("bad signature"))
    def test_connect_host_cli_reports_connector_errors_without_traceback(self, _connect):
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = setup_main(["connect-host", "--url", "http://127.0.0.1:8780"])

        self.assertEqual(result, 2)
        self.assertIn("ERROR: bad signature", stderr.getvalue())


class TvRemoteTests(unittest.TestCase):
    def test_remote_parser_accepts_bounded_key_count(self):
        args = build_remote_parser().parse_args(
            [
                "--url",
                "http://192.168.1.50",
                "--profile",
                "lg-tv-ir-v1",
                "press",
                "volume_up",
                "--count",
                "3",
            ]
        )

        self.assertEqual(args.key, "VOLUME_UP")
        self.assertEqual(args.count, 3)

    def test_remote_profile_is_optional_after_setup(self):
        args = build_remote_parser().parse_args(
            ["--url", "http://192.168.1.50", "press", "mute"]
        )

        self.assertIsNone(args.profile)

    def test_remote_dispatch_uses_authenticated_client_interface(self):
        client = mock.Mock()
        client.press_key.return_value = {"status": "executed"}

        result = dispatch(client, "VOLUME_UP", count=2)

        self.assertEqual(result, {"status": "executed"})
        client.press_key.assert_called_once_with("VOLUME_UP", count=2)

    def test_remote_discovers_one_matching_connector_when_url_is_omitted(self):
        records = [
            {"url": "http://192.168.1.41", "profile": "sony-bravia-ir-v1"},
            {"url": "http://192.168.1.50", "profile": "lg-tv-ir-v1"},
        ]

        url = resolve_connector_url(
            None,
            profile="lg-tv-ir-v1",
            timeout=0.1,
            discover=lambda _timeout: records,
        )

        self.assertEqual(url, "http://192.168.1.50")

    def test_remote_requires_explicit_url_when_multiple_profiles_match(self):
        records = [
            {"url": "http://192.168.1.50", "profile": "lg-tv-ir-v1"},
            {"url": "http://192.168.1.51", "profile": "lg-tv-ir-v1"},
        ]

        with self.assertRaisesRegex(ProtocolError, "More than one"):
            resolve_connector_url(
                None,
                profile="lg-tv-ir-v1",
                timeout=0.1,
                discover=lambda _timeout: records,
            )

    def test_remote_rejects_non_finite_discovery_timeout(self):
        discover = mock.Mock(return_value=[])

        with self.assertRaisesRegex(ProtocolError, "between 0 and 60"):
            resolve_connector_url(
                None,
                profile="lg-tv-ir-v1",
                timeout=float("nan"),
                discover=discover,
            )

        discover.assert_not_called()


class IphoneShortcutSetupTests(unittest.TestCase):
    def test_manual_shortcut_token_requires_explicit_reveal(self):
        hidden = StringIO()
        with redirect_stdout(hidden):
            _print_shortcut_values("https://voice.example.ts.net", "private-token")

        revealed = StringIO()
        with redirect_stdout(revealed):
            _print_shortcut_values(
                "https://voice.example.ts.net",
                "private-token",
                reveal_token=True,
            )

        self.assertNotIn("private-token", hidden.getvalue())
        self.assertIn("Bearer ***", hidden.getvalue())
        self.assertIn("Bearer private-token", revealed.getvalue())


if __name__ == "__main__":
    unittest.main()
