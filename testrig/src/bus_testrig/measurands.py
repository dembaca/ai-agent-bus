"""The measurands the runs collect, read from `measurands.toml`.

Nothing in the harness spells a measurand's name: every name comes from the
file beside this package (condition 3 of the build order's approval). A name
that a run collects but the file does not declare is a bug, and so is a
declared name the report does not carry.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# src/bus_testrig/measurands.py -> testrig/measurands.toml
FILE = Path(__file__).resolve().parents[2] / "measurands.toml"

MEASURED = "measured"
NOT_YET = "not_yet_measurable"


@dataclass(frozen=True)
class Measurand:
    name: str
    source: str
    unit: str
    status: str
    what: str
    why: str = ""

    @property
    def measurable(self) -> bool:
        return self.status == MEASURED


def load(path: Path = FILE) -> list[Measurand]:
    """The file's measurands in order. Raises on a missing or malformed file
    and on a status the harness does not know: a run must never silently
    report a number under an unknown label."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    rows = data.get("measurand")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: no [[measurand]] entries")
    out: list[Measurand] = []
    seen: set[str] = set()
    for row in rows:
        for key in ("name", "source", "unit", "status", "what"):
            if not row.get(key):
                raise ValueError(f"{path}: a measurand without {key!r}: {row}")
        if row["status"] not in (MEASURED, NOT_YET):
            raise ValueError(f"{path}: {row['name']}: unknown status {row['status']!r}")
        if row["status"] == NOT_YET and not row.get("why"):
            raise ValueError(f"{path}: {row['name']}: not_yet_measurable needs a 'why'")
        if row["name"] in seen:
            raise ValueError(f"{path}: {row['name']} declared twice")
        seen.add(row["name"])
        out.append(Measurand(name=row["name"], source=row["source"], unit=row["unit"],
                             status=row["status"], what=row["what"], why=row.get("why", "")))
    return out


def names(path: Path = FILE) -> list[str]:
    return [m.name for m in load(path)]
