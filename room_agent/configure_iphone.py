"""Enroll, rotate, list, or revoke iPhone Action Button clients."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from iphone_gateway import (
    IphoneDeviceRegistry,
    IphoneGatewayError,
    default_iphone_registry_path,
)


DEFAULT_REGISTRY_FILE = default_iphone_registry_path()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-file", type=Path, default=DEFAULT_REGISTRY_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll = subparsers.add_parser("enroll", help="Create a new iPhone credential.")
    enroll.add_argument("--name", default="My iPhone")
    enroll.add_argument(
        "--base-url",
        required=True,
        help="Private HTTPS base URL from Tailscale Serve, without a trailing slash.",
    )
    enroll.add_argument(
        "--show-token",
        action="store_true",
        help="Print the one-time bearer token for manual iPhone Shortcut setup.",
    )

    pair = subparsers.add_parser(
        "pair",
        help="Create a short-lived one-use pairing link for the Room Agent app.",
    )
    pair.add_argument("--name", default="iPhone")
    pair.add_argument(
        "--base-url",
        required=True,
        help="Private HTTPS base URL from Tailscale Serve, without a trailing slash.",
    )
    pair.add_argument(
        "--ttl-seconds",
        type=int,
        default=600,
        help="Pairing-link lifetime (30-3600 seconds; default: 600).",
    )
    pair.add_argument(
        "--qr-file",
        type=Path,
        help="Optionally save the one-use app link as an SVG QR code.",
    )

    rotate = subparsers.add_parser("rotate", help="Replace a device credential.")
    rotate.add_argument("device_id")
    rotate.add_argument("--base-url", required=True)
    rotate.add_argument(
        "--show-token",
        action="store_true",
        help="Print the replacement token for manual iPhone Shortcut setup.",
    )

    revoke = subparsers.add_parser("revoke", help="Immediately reject a device.")
    revoke.add_argument("device_id")

    subparsers.add_parser("list", help="List enrolled and revoked devices.")
    return parser


def _validated_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise IphoneGatewayError(
            "--base-url must be a root HTTPS URL such as "
            "https://voiceagent.example.ts.net"
        )
    return base_url.rstrip("/")


def _print_shortcut_values(
    base_url: str,
    token: str,
    *,
    reveal_token: bool = False,
) -> None:
    url = base_url + "/v1/commands"
    print("Enter these values into the private Room Agent Shortcut:")
    print(f"URL: {url}")
    if reveal_token:
        print(f"Authorization: Bearer {token}")
        print("This credential is shown once. Rotate it if it is copied or exposed.")
    else:
        print("Authorization: Bearer ***")
        print("The token is hidden. Re-enroll with --show-token for manual Shortcut setup.")


def _pairing_link(base_url: str, code: str) -> str:
    return "roomagent://pair?" + urlencode({"server": base_url, "code": code})


def _save_qr(link: str, path: Path) -> Path:
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError as exc:
        raise IphoneGatewayError(
            "QR generation requires the Windows dependencies to be installed."
        ) from exc
    destination = path.expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = qrcode.make(link, image_factory=SvgPathImage)
        image.save(destination)
    except OSError as exc:
        raise IphoneGatewayError(f"The pairing QR code cannot be saved: {exc}") from exc
    return destination


def main() -> int:
    args = build_parser().parse_args()
    registry = IphoneDeviceRegistry(args.registry_file)
    try:
        if args.command == "enroll":
            base_url = _validated_base_url(args.base_url)
            enrollment = registry.enroll(args.name)
            print(f"Enrolled {enrollment.name} ({enrollment.device_id}).")
            _print_shortcut_values(
                base_url,
                enrollment.token,
                reveal_token=args.show_token,
            )
        elif args.command == "pair":
            base_url = _validated_base_url(args.base_url)
            invitation = registry.create_pairing(
                args.name,
                ttl_seconds=args.ttl_seconds,
            )
            link = _pairing_link(base_url, invitation.code)
            print("Room Agent app pairing is ready.")
            print(f"Expires: {invitation.expires_at}")
            print(f"Open on iPhone: {link}")
            if args.qr_file is not None:
                qr_path = _save_qr(link, args.qr_file)
                print(f"QR code: {qr_path}")
            print(
                "The link is one-use and short-lived; it contains no permanent "
                "device credential."
            )
        elif args.command == "rotate":
            base_url = _validated_base_url(args.base_url)
            enrollment = registry.rotate(args.device_id)
            print(f"Rotated {enrollment.name} ({enrollment.device_id}).")
            _print_shortcut_values(
                base_url,
                enrollment.token,
                reveal_token=args.show_token,
            )
        elif args.command == "revoke":
            registry.revoke(args.device_id)
            print(f"Revoked {args.device_id}.")
        else:
            devices = registry.list_devices()
            if not devices:
                print("No iPhones are enrolled.")
            for device in devices:
                status = "revoked" if device["revoked_at"] else "active"
                print(f"{device['id']}  {status:7}  {device['name']}")
    except IphoneGatewayError as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
