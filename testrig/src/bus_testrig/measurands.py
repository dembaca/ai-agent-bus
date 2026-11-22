"""The measurands the runs collect, read from `measurands.toml`.

A measurand has a fixed `id` and a free `name` (review point 8): the harness
computes and reports by `id`, only the printed report uses `name`. Renaming
therefore cannot break a run silently, and a run that declares an `id` no
formula produces is a hard error, not a silent zero.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# The source tree keeps it beside the package (`testrig/measurands.toml`), a
# wheel carries it inside (`bus_testrig/measurands.toml`, pyproject's
# force-include): both are looked for, source tree first.
FILE = next(p for p in (_HERE.parents[1] / "measurands.toml", _HERE / "measurands.toml")
            if p.exists())

MEASURED = "measured"
NOT_YET = "not_yet_measurable"


@dataclass(frozen=True)
class Measurand:
    id: str
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
    """The file's measurands in order.

    Raises on a missing or malformed file, on an unknown status, and on an
    `id` declared twice: a run must never report a number under an unknown
    label or let one id stand for two rows.
    """
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    rows = data.get("measurand")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: no [[measurand]] entries")
    out: list[Measurand] = []
    seen: set[str] = set()
    for row in rows:
        for key in ("id", "name", "source", "unit", "status", "what"):
            if not row.get(key):
                raise ValueError(f"{path}: a measurand without {key!r}: {row}")
        if row["status"] not in (MEASURED, NOT_YET):
            raise ValueError(f"{path}: {row['id']}: unknown status {row['status']!r}")
        if row["status"] == NOT_YET and not row.get("why"):
            raise ValueError(f"{path}: {row['id']}: not_yet_measurable needs a 'why'")
        if row["id"] in seen:
            raise ValueError(f"{path}: id {row['id']!r} declared twice")
        seen.add(row["id"])
        out.append(Measurand(id=row["id"], name=row["name"], source=row["source"],
                             unit=row["unit"], status=row["status"], what=row["what"],
                             why=row.get("why", "")))
    return out


def ids(path: Path = FILE) -> list[str]:
    return [m.id for m in load(path)]
