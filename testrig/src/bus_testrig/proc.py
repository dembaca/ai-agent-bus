"""The faults that belong to the operating system, not to a proxy.

Ending a process and cutting a process's network are not things a harness can
produce honestly from inside: a fake kill leaves the victim running, and a fake
cut leaves the socket usable. Both are done for real here, on processes this
harness started.

  kill      SIGKILL, then wait for the process to be gone (by its exit status
            when the caller can observe it, else by pid liveness)
  pause     SIGSTOP: the process hangs with its sockets open (`resume` ends it)
  blackhole and cut are the proxy's (`proxy.FrameProxy`): a blackhole keeps the
            socket open and silent, a cut aborts the transport with no close
            frame. Neither needs the operating system, so `proc` does not offer
            them, and `network_cut_supported` says so.

Cutting one process's network in a way that leaves other processes alone
needs a network namespace per process, which this harness does not implement
(and would not, without asking for root). A run cuts the connection through
the proxy instead, which is the part a peer observes.
"""

from __future__ import annotations

import os
import signal
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
    """`kill` for a `subprocess.Popen` this harness owns.

    Waits for the child to be reaped, so a zombie does not count as a survivor,
    and reports the child's **real** return code: a process that had already
    ended with its own code keeps it, and only one this call actually killed
    reports the signal. Reporting `-sig` for both hid which had happened.
    """
    before = child.poll()
    if before is not None:
        return Killed(returncode=before, waited_s=0.0)
    result = kill(child.pid, calls=lambda: child.poll() is not None)
    return Killed(returncode=child.returncode if child.returncode is not None
                  else result.returncode, waited_s=result.waited_s)


def pause(pid: int) -> None:
    """Hang a process the harness started: SIGSTOP, so it neither runs nor exits.

    Its sockets stay open and the kernel still accepts bytes for it until the
    buffers fill: what a peer observes is a server that stopped answering, not
    one that went away. `resume` ends the hang. Raises `Unsupported` where the
    signal cannot be sent; a process already gone is not an error (there is
    nothing left to hang, and the caller's next step finds out).
    """
    _signal(pid, signal.SIGSTOP if sys.platform != "win32" else 0, "SIGSTOP")


def resume(pid: int) -> None:
    """End a `pause`: SIGCONT."""
    _signal(pid, signal.SIGCONT if sys.platform != "win32" else 0, "SIGCONT")


def _signal(pid: int, sig: int, name: str) -> None:
    if sys.platform == "win32":
        raise Unsupported(name, "a POSIX signal is not available here")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise Unsupported(name, f"no permission on pid {pid}: {exc}") from exc


def network_cut_supported() -> str:
    """This harness does not cut a process's network itself: that needs a
    network namespace per process, and on anything but Linux without root it
    would take other processes with it. A run cuts the *connection* through the
    proxy instead (`proxy`, `--fault cut`), which needs nothing from the OS and
    is what a peer can actually observe. This function says so, so no caller
    believes a namespace cut exists here.
    """
    return ("cut the connection through the proxy (--fault cut); "
            "a per-process network namespace is not implemented here")
