"""Explicit synthetic-only resource probe; never enables a pilot or calls APIs.

Run against a DISABLED staged service for aggregate cgroup evidence. Local mode
uses import-resident service processes and cannot qualify a production gate.
No credentials are inherited by probe children; network syscalls are denied.
The representative fixtures are not a proof for every hostile compressed PDF.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

MIB = 1024 * 1024
BUDGET_BYTES = 512 * MIB
SAFETY_MARGIN_BYTES = 64 * MIB
# One API upload: retained chunks plus joined body, while a worker processes a
# different report. API must reject a second upload before reading its body.
UPLOAD_BUFFER_BYTES = 2 * 10 * MIB
API_MODULE = "api.twilio_review_pilot"
WORKER_MODULE = "scripts.twilio_review_pilot_worker"
CASES = ("native_pdf_10mib", "scan_pdf_10mib", "photo_png_5mib", "photo_jpeg_5mib")
MAX_PROTOCOL_BYTES = 64 * 1024
ADMISSION_HEADROOM_BYTES = 192 * MIB


class CgroupMemoryGuard:
    """Fail-closed admission/watchdog; not a hard descendant memory partition.

    Construct one per media evaluation. No network, secrets, writes or providers.
    The outer service cgroup remains the hard 512 MiB ceiling; a sudden native
    allocation can outrun polling, so restart/unknown-send recovery is mandatory.
    """
    def __init__(self, root=Path("/sys/fs/cgroup")):
        self.root = Path(root)
        self.initial_oom = None

    def __call__(self, phase):
        state = cgroup_snapshot(self.root)
        if not isinstance(phase, str) or phase not in {"admission", "running"} or state is None:
            return False
        limit = state["maximum"]
        if limit is None or not 0 < limit <= BUDGET_BYTES:
            return False
        current_oom = (state["oom"], state["oom_kill"])
        if self.initial_oom is None:
            if phase != "admission":
                return False
            self.initial_oom = current_oom
        if current_oom != self.initial_oom:
            return False
        required = ADMISSION_HEADROOM_BYTES if phase == "admission" else SAFETY_MARGIN_BYTES
        return limit - state["current"] >= required


def cgroup_snapshot(root=Path("/sys/fs/cgroup")):
    """Read v2 whole-cgroup counters; never substitute process RSS for them."""
    root = Path(root)
    try:
        maximum = (root / "memory.max").read_text().strip()
        cpu = (root / "cpu.max").read_text().split()
        events = dict(line.split() for line in (root / "memory.events").read_text().splitlines())
        result = {
            "current": int((root / "memory.current").read_text()),
            "maximum": None if maximum == "max" else int(maximum),
            "cpu_cores": None if cpu[0] == "max" else int(cpu[0]) / int(cpu[1]),
            "oom": int(events["oom"]), "oom_kill": int(events["oom_kill"]),
        }
        if result["current"] < 0 or result["oom"] < 0 or result["oom_kill"] < 0:
            return None
        try:
            result["historical_peak"] = int((root / "memory.peak").read_text())
        except (OSError, ValueError):
            result["historical_peak"] = None
        return result
    except (OSError, ValueError, ZeroDivisionError, IndexError, KeyError):
        return None


def process_tree(root_pid, proc_root=Path("/proc")):
    pending, found = [root_pid], set()
    parent_map = None
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            pending.extend(int(x) for x in (Path(proc_root) / str(pid) / "task" / str(pid) / "children").read_text().split())
        except (OSError, ValueError):
            # Some restricted Linux /proc mounts omit task/*/children. PPID
            # from stat still exposes descendants; never silently report only
            # the harness as though it were the whole diagnostic process tree.
            if parent_map is None:
                parent_map = {}
                for stat_path in Path(proc_root).glob("[0-9]*/stat"):
                    try:
                        fields = stat_path.read_text().rpartition(")")[2].split()
                        parent_map.setdefault(int(fields[1]), []).append(int(stat_path.parent.name))
                    except (OSError, ValueError, IndexError):
                        continue
            pending.extend(parent_map.get(pid, []))
    return found


def process_rss_bytes(pids):
    total = 0
    for pid in pids:
        try:
            line = next(x for x in Path(f"/proc/{pid}/status").read_text().splitlines() if x.startswith("VmRSS:"))
            total += int(line.split()[1]) * 1024
        except (OSError, ValueError, StopIteration):
            pass
    return total  # Diagnostic only: shared pages double-count; page cache absent.


def stop_process_tree(process):
    # OCR creates its own session. Killing only the outer worker's group would
    # leave it alive; enumerate descendants before stopping any of their parents.
    descendants = process_tree(process.pid)
    for pid in sorted(descendants, reverse=True):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def staged_service_identity(proc_root=Path("/proc")):
    """Exactly one API/worker in our cgroup, identified across PID reuse/restart."""
    found = {API_MODULE: [], WORKER_MODULE: []}
    try:
        own_cgroup = (Path(proc_root) / "self" / "cgroup").read_bytes()
    except OSError:
        return None
    for path in Path(proc_root).glob("[0-9]*/cmdline"):
        try:
            args = path.read_bytes().split(b"\0")
            if not ((API_MODULE + ":app").encode() in args or WORKER_MODULE.encode() in args):
                continue
            if (path.parent / "cgroup").read_bytes() != own_cgroup:
                continue
            # Field 22 is starttime; comm may contain spaces or parentheses.
            starttime = int((path.parent / "stat").read_text().rpartition(")")[2].split()[19])
            identity = (int(path.parent.name), starttime)
        except (OSError, ValueError, IndexError):
            continue
        if (API_MODULE + ":app").encode() in args:
            found[API_MODULE].append(identity)
        if WORKER_MODULE.encode() in args:
            found[WORKER_MODULE].append(identity)
    return {name: items[0] for name, items in found.items()} if all(len(items) == 1 for items in found.values()) else None


def disabled_service_processes_present(proc_root=Path("/proc")):
    return staged_service_identity(proc_root) is not None


def qualification(report):
    """Conservative measured-staging gate, not a universal memory guarantee."""
    reasons = []
    if report["mode"] != "attached-disabled" or not report["service_processes_present"]:
        reasons.append("Actual disabled API and worker were not observed; local diagnostic only.")
    if not report.get("service_continuity_verified", False):
        reasons.append("Exactly one API and worker in this cgroup were not continuously observed through the probe.")
    first, last = report["cgroup_before"], report["cgroup_after"]
    if first is None or last is None:
        reasons.append("Readable cgroup v2 memory/CPU/OOM evidence is unavailable.")
    else:
        if first["maximum"] != BUDGET_BYTES or last["maximum"] != BUDGET_BYTES:
            reasons.append("The measured cgroup was not constrained to exactly 512 MiB.")
        if any(state["cpu_cores"] is None or not 0 < state["cpu_cores"] <= 0.5 for state in (first, last)):
            reasons.append("The measured cgroup was not constrained to at most 0.5 CPU.")
        if last["oom"] != first["oom"] or last["oom_kill"] != first["oom_kill"]:
            reasons.append("Cgroup OOM counters changed during the probe.")
        if any(type(state.get("historical_peak")) is not int or state["historical_peak"] <= 0 for state in (first, last)):
            reasons.append("Kernel memory.peak evidence is unavailable; sampled RSS/current cannot prove the measured aggregate margin.")
        elif last["historical_peak"] < first["historical_peak"]:
            reasons.append("Kernel memory.peak decreased; an intervening reset invalidates this measured gate.")
        peak = max(report["sampled_cgroup_peak_bytes"], last.get("historical_peak") or 0)
        if peak > BUDGET_BYTES - SAFETY_MARGIN_BYTES:
            reasons.append("Measured aggregate memory lacks the required 64 MiB safety margin.")
    if report["minimum_free_temp_bytes"] < 128 * MIB:
        reasons.append("Less than 128 MiB of temporary filesystem space remained.")
    if not report.get("temporary_preflight_passed", False):
        reasons.append("At least 256 MiB free temporary space was not verified before generating fixtures.")
    if len(report["cases"]) != len(CASES) or any(not item["completed"] for item in report["cases"]):
        reasons.append("One or more bounded synthetic cases failed or were stopped; input support is not qualified.")
    return reasons


def supported_case(case, assessment):
    if assessment.get("rows") != 7:
        return False
    if case == "native_pdf_10mib":
        return assessment.get("automatic_allowed") is True and assessment.get("review_only") is False
    return (assessment.get("automatic_allowed") is False and assessment.get("review_only") is True
            and assessment.get("complete_cells") == [49, 49]
            and len(assessment.get("numeric_tokens", [])) == 2
            and all(value > 0 for value in assessment["numeric_tokens"]))


def _safe_env(directory):
    from services.forecast_media_worker import _safe_environment
    return dict(_safe_environment(str(directory)), PILOT_ENABLED="0", REVIEW_PILOT_ENABLED="0",
                PILOT_REVIEW_ENABLED="0", PILOT_REVIEW_RESOURCE_CONFIRMED="0", PILOT_VOLUME_CONFIRMED="0")


def _command(*args):
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), *args]


def _pad_file(path, size):
    remaining = size - path.stat().st_size
    if remaining < 0:
        raise ValueError("Synthetic fixture exceeded its source limit")
    with path.open("ab") as stream:
        while remaining:
            count = min(remaining, MIB)
            stream.write(b"\n" * count)
            remaining -= count


def _generate_fixtures(directory, reference):
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    start = reference.date() - timedelta(days=(reference.weekday() - 4) % 7)
    lines = ["Synthetic forecast - fictional data only", "Hotel code: RESOURCE-SYNTHETIC",
             "Dates: " + " ".join((start + timedelta(days=i)).isoformat() for i in range(7))]
    for label, values in (
        ("Departures", "0 20 30 40 50 60 70"), ("Stayovers", "0 40 50 60 70 80 90"),
        ("Arrivals", "0 30 40 50 60 70 80"), ("Rooms on books", "0 70 90 110 130 150 170"),
        ("Forecast pickup", "0 10 10 10 10 10 10"), ("Forecast occupied", "0 80 100 120 140 160 180"),
        ("Occupancy %", "0 40 50 60 70 80 90"),
    ):
        lines.append(label + ": " + values)
    native = Path(directory) / CASES[0]
    document = canvas.Canvas(str(native), pagesize=(1200, 700), invariant=1)
    for i, line in enumerate(lines):
        document.drawString(30, 650 - i * 55, line)
    document.save()
    image = Image.new("RGB", (4000, 3000), "white")
    draw = ImageDraw.Draw(image)
    # Large, clearly legible source typography; stress decoded/encoded size,
    # not whether a small photocopied zero resembles an O. Counts stay literal.
    font = ImageFont.truetype("DejaVuSans.ttf", 64)
    for i, line in enumerate(lines):
        draw.text((70, 100 + i * 220), line, font=font, fill="black")
    image.save(Path(directory) / CASES[2], format="PNG")
    image.save(Path(directory) / CASES[3], format="JPEG", quality=90)
    scan = canvas.Canvas(str(Path(directory) / CASES[1]), pagesize=image.size, invariant=1)
    scan.drawImage(ImageReader(image), 0, 0, width=image.width, height=image.height)
    scan.save()
    image.close()
    for name in CASES:
        _pad_file(Path(directory) / name, (10 if "pdf" in name else 5) * MIB)


def _child(operation, directory, case=None, reference=None):
    # Called only in an isolated fresh executable with a sanitized environment.
    from services.forecast_media_worker import _install_network_denial, evaluate_forecast_media_isolated
    _install_network_denial()
    if operation == "--generate":
        _generate_fixtures(directory, datetime.fromisoformat(reference))
        return 0
    if operation == "--resident-api":
        importlib.import_module(API_MODULE)
        retained_upload = bytearray(UPLOAD_BUFFER_BYTES)
        while True:
            time.sleep(60)
    if operation == "--assess":
        importlib.import_module(WORKER_MODULE)
        from services.owner_review_runtime import warm_owner_dependencies
        warm_owner_dependencies()
        # In attached mode the real API is idle. Account conservatively for a
        # simultaneous maximum intake in this helper instead, without any HTTP.
        retained_upload = bytearray(UPLOAD_BUFFER_BYTES)
        data = (Path(directory) / case).read_bytes()
        mime = "application/pdf" if "pdf" in case else "image/png" if "png" in case else "image/jpeg"
        guard = CgroupMemoryGuard() if os.getenv("REVIEW_RESOURCE_GUARD") == "1" else None
        result = evaluate_forecast_media_isolated(data, mime, "RESOURCE-SYNTHETIC", 200,
                                                 datetime.fromisoformat(reference), memory_guard=guard)
        print(json.dumps({"method": result["method"], "review_only": result["requires_review"],
                          "automatic_allowed": result["automatic_roster_allowed"],
                          "rows": len(result["rows"]), "child_metrics": result["isolation"],
                          "complete_cells": [x["complete_cells"] for x in result.get("evidence", [])],
                          "numeric_tokens": [x["numeric_tokens"] for x in result.get("evidence", [])]}))
        return 0
    return 2


def run_probe(mode, *, temp_parent=None, cgroup_root=Path("/sys/fs/cgroup")):
    if mode not in {"local", "attached-disabled"}:
        raise ValueError("Choose a supported synthetic probe mode.")
    if any(os.getenv(name, "0") != "0" for name in ("PILOT_ENABLED", "PILOT_REVIEW_ENABLED", "REVIEW_PILOT_ENABLED")):
        raise ValueError("Disable the pilot before running the synthetic resource probe.")
    reference = datetime.now(timezone.utc).isoformat()
    initial_service_identity = staged_service_identity()
    service_lost = False
    report = {"synthetic_only": True, "mode": mode,
              "service_processes_present": initial_service_identity is not None,
              "service_continuity_verified": False,
              "cgroup_before": cgroup_snapshot(cgroup_root), "cgroup_after": None,
              "sampled_cgroup_peak_bytes": 0, "diagnostic_process_tree_rss_peak_bytes": 0,
              "minimum_free_temp_bytes": 2**63, "upload_buffer_bytes": UPLOAD_BUFFER_BYTES,
              "temporary_preflight_passed": False,
              "cases": [], "limits_are_not_aggregate_guarantee": True}
    resident = None
    with tempfile.TemporaryDirectory(prefix="synthetic-review-resources-", dir=temp_parent) as directory:
        os.chmod(directory, 0o700)
        initial_free = shutil.disk_usage(directory).free
        report["temporary_preflight_passed"] = initial_free >= 256 * MIB
        report["minimum_free_temp_bytes"] = initial_free
        def sample():
            nonlocal service_lost
            if mode == "attached-disabled" and (initial_service_identity is None or staged_service_identity() != initial_service_identity):
                service_lost = True
            state = cgroup_snapshot(cgroup_root)
            if state:
                report["sampled_cgroup_peak_bytes"] = max(report["sampled_cgroup_peak_bytes"], state["current"])
            report["diagnostic_process_tree_rss_peak_bytes"] = max(
                report["diagnostic_process_tree_rss_peak_bytes"], process_rss_bytes(process_tree(os.getpid())))
            report["minimum_free_temp_bytes"] = min(report["minimum_free_temp_bytes"], shutil.disk_usage(directory).free)
            return (service_lost or report["minimum_free_temp_bytes"] < 128 * MIB
                    or (state is not None and state["current"] > BUDGET_BYTES - SAFETY_MARGIN_BYTES))
        def execute(arguments, deadline):
            with tempfile.TemporaryFile(dir=directory) as output:
                env = dict(_safe_env(directory), REVIEW_RESOURCE_GUARD="1" if mode == "attached-disabled" else "0")
                process = subprocess.Popen(_command(*arguments), env=env, cwd=directory,
                                           stdout=output, stderr=subprocess.DEVNULL, start_new_session=True)
                started = time.monotonic()
                stopped = False
                try:
                    while process.poll() is None:
                        if sample() or time.monotonic() - started > deadline:
                            stopped = True
                            break
                        time.sleep(0.02)
                    sample()
                    if stopped or process.poll() != 0:
                        return False, {}, round(time.monotonic() - started, 3)
                    output.seek(0)
                    raw = output.read(MAX_PROTOCOL_BYTES + 1)
                    if len(raw) > MAX_PROTOCOL_BYTES:
                        return False, {}, round(time.monotonic() - started, 3)
                    return True, json.loads(raw) if raw else {}, round(time.monotonic() - started, 3)
                except (ValueError, OSError):
                    return False, {}, round(time.monotonic() - started, 3)
                finally:
                    stop_process_tree(process)
        try:
            ok = False
            if report["temporary_preflight_passed"]:
                ok, _, _ = execute(("--generate", directory, reference), 30)
            if ok:
                if mode == "local":
                    resident = subprocess.Popen(_command("--resident-api", directory), env=_safe_env(directory),
                                                cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                start_new_session=True)
                    # The local resident is a diagnostic, not an HTTP server.
                    time.sleep(0.5)
                for case in CASES:
                    success, measurements, seconds = execute(("--assess", directory, case, reference), 45)
                    success = success and supported_case(case, measurements)
                    if mode == "local" and resident.poll() is not None:
                        success = False
                    report["cases"].append({"name": case, "completed": success, "seconds": seconds,
                                            "assessment": measurements})
                    if not success:
                        break  # No resource retry loop or hidden unsupported case.
        finally:
            if resident is not None:
                stop_process_tree(resident)
            report["cgroup_after"] = cgroup_snapshot(cgroup_root)
            report["service_continuity_verified"] = (mode == "attached-disabled" and initial_service_identity is not None
                and not service_lost and staged_service_identity() == initial_service_identity)
    report["blocking_reasons"] = qualification(report)
    report["measured_stage_gate_passed"] = not report["blocking_reasons"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-synthetic", action="store_true", help="Explicitly opt into bounded synthetic local work")
    parser.add_argument("--mode", choices=("local", "attached-disabled"), default="local")
    parser.add_argument("--temporary-parent", type=Path)
    args = parser.parse_args(argv)
    if not args.run_synthetic:
        parser.error("Nothing runs without --run-synthetic; the pilot must remain disabled.")
    try:
        report = run_probe(args.mode, temp_parent=args.temporary_parent)
    except Exception:
        print(json.dumps({"measured_stage_gate_passed": False, "error": "Synthetic probe could not safely run; no provider was contacted."}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["measured_stage_gate_passed"] else 2


if __name__ == "__main__":
    # -I omits the repository path; explicitly use this root-owned source tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if len(sys.argv) > 1 and sys.argv[1] in {"--generate", "--resident-api", "--assess"}:
        try:
            if sys.argv[1] == "--generate":
                code = _child(sys.argv[1], sys.argv[2], reference=sys.argv[3])
            elif sys.argv[1] == "--assess":
                code = _child(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
            else:
                code = _child(sys.argv[1], sys.argv[2])
        except Exception:
            code = 2  # Never log media, secrets, native stderr or exception text.
        raise SystemExit(code)
    raise SystemExit(main())
