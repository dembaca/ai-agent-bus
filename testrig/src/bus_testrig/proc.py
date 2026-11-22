"""The faults that belong to the operating system, not to a proxy.

Ending a process and cutting a process's network are not things a harness can
produce honestly from inside: a fake kill leaves the victim running, and a fake
cut leaves the socket usable. Both are done for real here, on processes this
harness started.

  kill      SIGKILL, then wait for the process to be gone and check that the
            other side sees it (the exit code and the closed stream)
  blackhole the socket stays open and nothing passes: a peer that believes it
            is connected to a server that no longer answers

Cutting one process's network in a way that leaves other processes alone
needs a network namespace per process. On Linux a run can have it; on other
platforms it cannot without root, and this harness never asks for root. Where
it cannot, `network_cut_supported` says so and a run reports that the fault
was not produced, rather than reporting a number nobody measured.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable


class Unsupported(Exception):
    """This platform cannot produce the fault; a run reports it as such."""

    def __init__(self, fault: str, why: str) -> None:
        super().__init__(f"{fault}: {why}")
        self.fault = fault
        self.why = why


@dataclass(frozen=True)
class Killed:
    """What a kill left behind: the exit status and how long the rest took."""

    returncode: int
    waited_s: float


def kill(pid: int, *, sig: int = signal.SIGKILL, wait_s: float = 5.0,
         calls: Callable[[], bool] | None = None) -> Killed:
    """Hard-stop a process the harness started and wait until it stopped.

    `calls`, when given, is the caller's own test for "the process is gone"
    (usually `Popen.poll() is not None`, which also reaps it). Without it the
    check is `kill(pid, 0)`, which keeps answering for a child this process has
    not reaped yet: a killed child stays a zombie until its parent waits for
    it, and a zombie is not a running process. The caller that owns the child
    should pass `calls`.

    Raises `Unsupported` where a signal cannot be sent, and `TimeoutError` when
    the process survives the wait: a kill that did not take effect is not a
    fault a run may measure around.
    """
    if sys.platform == "win32":
        raise Unsupported("SIGKILL", "a POSIX signal is not available here")
    began = time.monotonic()
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone: the effect the fault wants holds
    except PermissionError as exc:
        raise Unsupported("SIGKILL", f"no permission on pid {pid}: {exc}") from exc
    while time.monotonic() - began < wait_s:
        if calls is not None:
            if calls():
                return Killed(returncode=-sig, waited_s=time.monotonic() - began)
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return Killed(returncode=-sig, waited_s=time.monotonic() - began)
        time.sleep(0.01)
    raise TimeoutError(f"pid {pid} survived {sig} for {wait_s}s")


def kill_child(child: Any) -> Killed:
    """`kill` for a `subprocess.Popen` this harness owns: waits for the child
    to be reaped, so a zombie does not count as a survivor."""
    return kill(child.pid, calls=lambda: child.poll() is not None)


def network_cut_supported() -> str:
    """'' when a run can cut one process's network, else why it cannot."""
    if sys.platform.startswith("linux"):
        return ""
    return (f"no per-process network namespace on {sys.platform}; "
            "cutting the network would take other processes with it")


def blackhole_supported() -> str:
    """The in-process variant of a cut is always available: it needs nothing
    but a socket this process owns."""
    return ""


def blackhole(sock: socket.socket) -> None:
    """Keep a socket open and pass nothing: the peer sees a live connection
    that never answers. Implemented by shrinking the buffers to their minimum
    and pausing the reader, which the caller controls: the socket is not
    closed, which is the point."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1)
