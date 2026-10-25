"""No services launched: deterministic process/signal/container contract tests."""
from pathlib import Path
import re
import signal
import subprocess
import sys

import pytest

from scripts import run_twilio_pilot as supervisor


class Child:
    def __init__(self, pid, code=None):
        self.pid, self.code, self.waited = pid, code, False
    def poll(self):
        return self.code
    def wait(self, timeout):
        self.waited = True
        if self.code is None:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        return self.code


class Harness:
    def __init__(self):
        self.now, self.children, self.launches, self.signals = 0.0, [], [], []
        self.handlers, self.previous = {}, {signal.SIGTERM: "old-term", signal.SIGINT: "old-int"}
        self.event, self.start_error, self.stop_on_term = None, False, True
    def popen(self, command, **kwargs):
        if self.start_error and self.children:
            raise OSError("synthetic-secret-must-not-be-logged")
        child = Child(100 + len(self.children))
        self.children.append(child)
        self.launches.append((command, kwargs))
        return child
    def register(self, number, handler):
        old = self.handlers.get(number, self.previous[number])
        self.handlers[number] = handler
        return old
    def sleep(self, seconds):
        self.now += seconds
        if self.event:
            event, self.event = self.event, None
            event()
    def killpg(self, pid, number):
        self.signals.append((pid, number, self.now))
        child = next(child for child in self.children if child.pid == pid)
        if number == signal.SIGKILL or self.stop_on_term:
            child.code = -number
    def run(self, env=None):
        return supervisor.supervise({} if env is None else env, popen=self.popen,
            clock=lambda: self.now, sleep=self.sleep, killpg=self.killpg, register_signal=self.register)


@pytest.mark.parametrize("port", ["0", "80", "1023", "65536", "-1", "+10000", "10000;bad", " 10000", "", "1.5", None, 10000])
def test_invalid_port_fails_without_starting_children(port, capsys):
    harness = Harness()
    assert harness.run({"PORT": port}) == 2
    assert not harness.launches and not harness.signals
    assert capsys.readouterr().err.strip() == "pilot_supervisor_invalid_port"


@pytest.mark.parametrize("port", ["1024", "10000", "65535"])
def test_valid_port_exact_single_worker_no_access_logs_or_proxy_headers(port):
    commands = supervisor.child_commands({"PORT": port})
    assert commands[0] == [sys.executable, "-m", "uvicorn", "api.twilio_pilot:app", "--host", "0.0.0.0", "--port", port, "--workers", "1", "--no-access-log", "--no-proxy-headers"]
    assert commands[1] == [sys.executable, "-m", "scripts.twilio_pilot_worker"]
    assert supervisor.child_commands({})[0][7] == "10000"


@pytest.mark.parametrize("which,code", [(0, 0), (1, 0), (0, 3), (1, -9)])
def test_any_unexpected_child_exit_stops_other_and_returns_failure(which, code, capsys):
    harness = Harness()
    harness.event = lambda: setattr(harness.children[which], "code", code)
    assert harness.run({"PILOT_DATA_ROOT": "/var/data/pilot", "TWILIO_AUTH_TOKEN": "unprinted"}) == 1
    assert len(harness.launches) == 2 and all(child.waited for child in harness.children)
    assert {pid for pid, sig, _ in harness.signals if sig == signal.SIGTERM} == {100, 101}
    assert all(kwargs["start_new_session"] and kwargs["env"]["PILOT_DATA_ROOT"] == "/var/data/pilot" for _, kwargs in harness.launches)
    assert harness.handlers == harness.previous
    assert "unprinted" not in capsys.readouterr().err


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_requested_shutdown_stops_both_groups_and_restores_handlers(number):
    harness = Harness()
    harness.event = lambda: harness.handlers[number](number, None)
    assert harness.run() == 0
    assert {pid for pid, sig, _ in harness.signals if sig == signal.SIGTERM} == {100, 101}
    assert all(child.waited for child in harness.children)
    assert harness.handlers == harness.previous


def test_stuck_children_are_killed_after_ten_seconds():
    harness = Harness()
    harness.stop_on_term = False
    harness.event = lambda: harness.handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert harness.run() == 0
    term = next(t for _, sig, t in harness.signals if sig == signal.SIGTERM)
    kill = next(t for _, sig, t in harness.signals if sig == signal.SIGKILL)
    assert kill - term == pytest.approx(10.0)
    assert all(child.waited for child in harness.children)


def test_second_launch_error_cleans_first_without_logging_exception(capsys):
    harness = Harness()
    harness.start_error = True
    assert harness.run() == 1
    assert len(harness.children) == 1 and harness.children[0].waited
    assert capsys.readouterr().err.strip() == "pilot_supervisor_start_failed"
    assert harness.handlers == harness.previous


def test_signal_during_first_spawn_prevents_second_launch():
    harness = Harness()
    original = harness.popen
    def spawn_then_stop(command, **kwargs):
        child = original(command, **kwargs)
        harness.handlers[signal.SIGTERM](signal.SIGTERM, None)
        return child
    harness.popen = spawn_then_stop
    assert harness.run() == 0
    assert len(harness.children) == 1 and harness.children[0].waited
    assert harness.handlers == harness.previous


def test_process_already_gone_is_harmless():
    child = Child(123, 0)
    def gone(*_):
        raise ProcessLookupError
    supervisor.stop_children([child], killpg=gone)
    assert child.waited


def test_main_sets_private_umask_before_start(monkeypatch):
    events = []
    monkeypatch.setattr(supervisor.os, "umask", lambda value: events.append(("umask", value)))
    monkeypatch.setattr(supervisor, "supervise", lambda: events.append(("start",)) or 0)
    assert supervisor.main() == 0 and events == [("umask", 0o077), ("start",)]


def test_container_and_blueprint_are_disabled_review_templates():
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile.pilot").read_text()
    blueprint = (root / "deploy/render-pilot.yaml.example").read_text()
    ignore = (root / ".dockerignore").read_text().splitlines()
    assert "USER 1000:1000" in docker and "chmod 0700 /var/data" in docker
    assert "COPY . " not in docker and "PILOT_ENABLED=0" in docker
    assert "scripts.run_twilio_pilot" in docker and "requirements-pilot.txt" in docker
    assert 'autoDeployTrigger: "off"' in blueprint and "plan: 0.5c-512mb" in blueprint
    assert "sizeGB: 1" in blueprint and "mountPath: /var/data" in blueprint
    assert "numInstances: 1" in blueprint and 'value: "0"' in blueprint
    for entry in (".git", ".env", "**/*.pdf", "**/*.sqlite*", "**/*.key", "data", "private", ".venv"):
        assert entry in ignore
    for line in (root / "requirements-pilot.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        assert name.lower() not in {"streamlit", "pytest", "pymupdf", "pytesseract"}
        assert re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version)
