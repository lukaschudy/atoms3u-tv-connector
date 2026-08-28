# Security policy

## Supported versions

Security fixes are applied to the latest revision of the `main` branch. This is
experimental hardware and software; there is no long-term support branch yet.

## Report a vulnerability

Please use GitHub's **Report a vulnerability** / private security advisory flow.
Do not open a public issue containing credentials, network addresses, exploit
steps, or a working proof of concept. Include the affected revision, impact,
and the smallest reproduction you can provide. You should receive an initial
response within seven days.

## Deployment boundaries

The connector is designed for a trusted local network:

- Never expose its HTTP endpoint directly to the public internet.
- Use a unique generated connector key and keep `tv-connector.key` and
  `include/secrets.h` private.
- Put the connector on an isolated home/IoT network when possible.
- A configured firmware image contains both the connector key and Wi-Fi
  credentials. If one is disclosed, rotate the connector key **and** the Wi-Fi
  password before flashing a newly prepared image.
- Never attach a locally configured `firmware.bin` or `firmware.elf` to a
  public issue or release. CI artifacts use documented placeholder credentials
  and are intended only as compile evidence, not ready-to-deploy firmware.
- Treat an `executed` response as transmission confirmation, not proof that a
  one-way USB or infrared action changed the TV.

Requests and successful execution responses are authenticated with
HMAC-SHA256. Requests are replay protected with timestamps and a fail-closed
nonce cache. Each boot also generates a random identifier that `/health`
advertises and every signed execution request must include. A captured request
from an earlier boot is therefore rejected even after the in-memory nonce cache
is cleared. The boot identifier and complete command schema are validated before
the nonce is inserted into the current boot's replay cache. A bounded HTTP parser
rejects oversized or ambiguous request framing before reading an execution body.
Command execution also fails closed until NTP establishes trusted time; an
authenticated request is never used to set the firmware clock. The connector
still belongs only on a trusted LAN.

Local HTTP does not encrypt traffic or conceal metadata. Unauthenticated health
and UDP discovery expose the connector name, profile, local IP, Wi-Fi signal,
boot/reset diagnostics, readiness, and the last emitted IR key. Choose a neutral
connector name and isolate it on a home/IoT network when possible. The firmware
accepts only bounded, typed remote operations; it does not execute shell
commands, arbitrary code, or caller-provided URLs.

PlatformIO is installed into a project-local virtual environment solely for
build, test, and flash commands. Do not expose the optional PlatformIO Home web
interface to a network; this project neither starts nor requires it.

Dependency-audit note (review by **2026-09-30**): PlatformIO 6.1.19 currently
constrains Starlette below 0.53. The published fixes for Starlette advisories
reported against that range require versions from 1.0.1 through 1.3.1. The
affected ASGI/web-server paths are not reachable in this project because setup
and CI invoke only PlatformIO build, test, and upload commands and never start
PlatformIO Home. Upgrade when PlatformIO publishes a compatible dependency
range; until then, do not run or expose `pio home`.
