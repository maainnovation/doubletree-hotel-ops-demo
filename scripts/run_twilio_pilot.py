"""Supervise the isolated pilot API and worker on one persistent-volume host.

No shell commands, credential setup or deployment happen here. Either child
exiting unexpectedly stops the whole service. The API's access log is disabled
because media paths are short-lived bearer capabilities.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time


def child_commands(environment):
    port = environment.get("PORT", "10000")
    if not isinstance(port, str) or not re.fullmatch(r"[0-9]{1,5}", port) or not 1024 <= int(port) <= 65535:
        raise ValueError("PORT must be an unprivileged TCP port between 1024 and 65535.")
    return [
        [sys.executable, "-m", "uvicorn", "api.twilio_pilot:app", "--host", "0.0.0.0",
         "--port", str(int(port)), "--workers", "1", "--no-access-log", "--no-proxy-headers"],
        [sys.executable, "-m", "scripts.twilio_pilot_worker"],
    ]


def stop_children(children, *, clock=time.monotonic, sleep=time.sleep, killpg=os.killpg):
    """Signal separate child process groups, allow 10 seconds, then force cleanup."""
    for child in children:
        try:
            killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = clock() + 10.0
    while any(child.poll() is None for child in children) and clock() < deadline:
        sleep(min(0.1, max(0, deadline - clock())))
    # Also clean up any descendants whose original child already exited.
    for child in children:
        try:
            killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass  # The host remains the final process/container cleanup boundary.


def supervise(environment=None, *, popen=subprocess.Popen, clock=time.monotonic,
              sleep=time.sleep, killpg=os.killpg, register_signal=signal.signal):
    environment = dict(os.environ if environment is None else environment)
    try:
        commands = child_commands(environment)
    except ValueError:
        print("pilot_supervisor_invalid_port", file=sys.stderr, flush=True)
        return 2
    requested_signal = [None]
    def request_stop(number, _frame):
        requested_signal[0] = number
    previous_handlers = {}
    children = []
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[number] = register_signal(number, request_stop)
        for command in commands:
            if requested_signal[0] is not None:
                return 0
            children.append(popen(command, env=environment, start_new_session=True))
        while requested_signal[0] is None:
            if any(child.poll() is not None for child in children):
                print("pilot_supervisor_child_exit", file=sys.stderr, flush=True)
                return 1
            sleep(0.1)
        return 0
    except (OSError, subprocess.SubprocessError):
        print("pilot_supervisor_start_failed", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_children(children, clock=clock, sleep=sleep, killpg=killpg)
        for number, previous in previous_handlers.items():
            register_signal(number, previous)


def main():
    # Inherited by both children: new database, PDF and token metadata are private.
    os.umask(0o077)
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
