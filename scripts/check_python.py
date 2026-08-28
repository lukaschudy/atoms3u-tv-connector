"""Fail clearly when connector setup is run with an unsupported Python."""

from __future__ import print_function

import importlib
import os
import sys

MINIMUM_VERSION = (3, 11)


def is_supported(version_info):
    return tuple(version_info[:2]) >= MINIMUM_VERSION


def installed_environment_errors(platform_name=None, importer=None):
    """Return native/import compatibility failures for the prepared environment."""

    platform_name = os.name if platform_name is None else platform_name
    importer = importlib.import_module if importer is None else importer
    modules = ["platformio", "bleak", "qrcode"]
    if platform_name == "nt":
        modules.append("winrt.runtime")
    errors = []
    for module in modules:
        try:
            importer(module)
        except (ImportError, OSError) as exc:
            errors.append("{}: {}".format(module, exc))
    return errors


def main():
    if not is_supported(sys.version_info):
        print(
            "Python 3.11 or newer is required; found {}.{}.".format(
                sys.version_info[0], sys.version_info[1]
            ),
            file=sys.stderr,
        )
        return 1
    if sys.argv[1:] == ["--installed"]:
        errors = installed_environment_errors()
        if errors:
            print(
                "The existing virtual environment is incompatible: {}".format(
                    "; ".join(errors)
                ),
                file=sys.stderr,
            )
            print("Remove .venv and run setup again.", file=sys.stderr)
            return 1
    elif sys.argv[1:]:
        print("Usage: check_python.py [--installed]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
