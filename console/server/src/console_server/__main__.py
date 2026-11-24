"""``python -m console_server``: the console service from the environment."""
from __future__ import annotations

import asyncio
import logging

from . import app, config


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = config.find()
    except config.ConfigError as exc:
        raise SystemExit(f"console: {exc}") from exc
    try:
        asyncio.run(app.serve(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
