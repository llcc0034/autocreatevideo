#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "autoprompt_backend.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - Django supplies this path
        raise ImportError("Django is not installed. Run: pip install -r requirements.txt") from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
