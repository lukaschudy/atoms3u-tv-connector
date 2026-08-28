# Public release checklist

The repository can remain private while this checklist is completed.

## Code and history

- [x] Working tree secrets and private settings are ignored.
- [x] Tracked files contain templates only, not configured credentials.
- [x] Basic history scan found no private keys or live API credentials.
- [x] Python tests and every PlatformIO environment pass locally.
- [ ] Review GitHub's secret-scanning result after visibility changes.
- [ ] If a real secret is found in history, rotate it first and rewrite history
  before announcing the repository.

## Project presentation

- [x] Root README starts with the standalone connector quick start.
- [x] License, contribution guide, code of conduct, security policy, changelog,
  issue forms, and pull-request template are present.
- [x] Compatibility claims distinguish compiled, simulated, and physically
  validated evidence.
- [x] Personal device names, workstation paths, and real private-network examples
  have been replaced with generic placeholders.
- [x] Add a clear product photo or wiring diagram with no private background data.
- [x] Use the public-facing `atoms3u-tv-connector` name in clone and badge links.

## GitHub settings

- [ ] Change repository visibility only after the checks above are green.
- [ ] Enable private vulnerability reporting, secret scanning, and Dependabot alerts.
- [ ] Add description: `Authenticated Wi-Fi, IR, and USB-HID TV remote for M5Stack ATOMS3U`.
- [ ] Add topics: `esp32-s3`, `m5stack`, `infrared`, `remote-control`, `smart-tv`,
  `platformio`, `home-automation`.
- [ ] Protect `main`: require pull requests and the Python/firmware checks.
- [ ] Disable wiki/projects if they will not be maintained.

## First release

- [ ] Complete at least one harmless-key physical acceptance run on each claimed TV model.
- [ ] Tag `v0.1.0` only after CI passes on the public default branch.
- [ ] Publish source only. Do not publish a credential-bearing firmware binary.
- [ ] Copy the Unreleased changelog section into the GitHub release notes.
