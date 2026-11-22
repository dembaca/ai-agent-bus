"""The faults that belong to the operating system, not to a proxy.

Ending a process and cutting a process's network are not things a harness can
fake honestly: a fake kill leaves the victim running, and a fake network cut
leaves the socket usable. Both are done for real here, on a process this
harness started, and only on that one:

  SIGKILL  the server gone without a chance to say anything
  net cut  the process keeps running, but its socket stops being its own

The cut is the one case that needs more than a signal: it is done by running
the process in its own network namespace where the platform allows it, and
otherwise by closing the socket from under it, which the caller must own. A
platform that offers neither is reported as such, never silently skipped.
"""

from __future__ import annotations

import os
import signal
import sys


class Unsupported(Exception):
    """This platform cannot produce the fault; the report names it as such."""

    def __init__(self, fault: str, why: str) -> None:
        super().__init__(f"{fault}: {why}")
        self.fault = fault
        self.why = why


def kill(pid: int, *, sig: int = signal.SIGKILL) -> None:
    """Hard-stop a process the harness started. Raises `Unsupported` where a
    signal cannot be sent, rather than pretending the stop happened."""
    if sys.platform == "win32":
        raise Unsupported("SIGKILL", "a POSIX signal is not available here")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone: the fault's effect holds
    except PermissionError as exc:
        raise Unsupported("SIGKILL", f"no permission on pid {pid}: {exc}") from exc


def network_cut_supported() -> str:
    """Whether this platform can cut one process's network: '' or why not.

    Linux namespaces can; macOS can not without root, and this harness never
    asks for root. The caller reports the honesty of the cut in its run."""
    if sys.platform.startswith("linux"):
        return ""
    return f"no per-process network namespace on {sys.platform}; the run cannot cut only this process's network"
