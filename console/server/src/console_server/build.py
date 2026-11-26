"""The console's build identity (#182): the release version and the
source commit, as the image build recorded them.

``build.json`` is written at image build by ``console/web/scripts/build-info.mjs``
into the web app's directory, and never served as a static file (the server
serves only ``index.html``, ``favicon.svg`` and ``assets/``). A missing file
is not an error (a run from source has no build); an unreadable one is
logged once. A value that does not look like a version or a commit is
dropped, so nothing odd reaches a label or a page.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("console_server.build")

VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
REVISION = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Build:
    version: str | None = None
    revision: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {"version": self.version, "revision": self.revision}


def _pick(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def read(web_dir: str) -> Build:
    """The build of the web app in `web_dir`; empty when there is none."""
    if not web_dir:
        return Build()
    file = Path(web_dir) / "build.json"
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Build()
    except (OSError, ValueError):
        log.warning("console: build.json is unreadable; no build shown")
        return Build()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        log.warning("console: build.json is not a JSON object; no build shown")
        return Build()
    return Build(version=_pick(data.get("version"), VERSION),
                 revision=_pick(data.get("revision"), REVISION))
