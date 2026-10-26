"""Killable Linux-only LOCAL forecast assessment, with no provider credentials.

The child has a wall deadline, per-process address-space/CPU/file limits and a
seccomp network deny-list inherited by Tesseract. This is NOT a complete hostile
native-code sandbox or an aggregate container-memory guarantee. Deployment still
needs host/container isolation, a concurrency limit and service-wide headroom.
"""
from __future__ import annotations

import base64
from datetime import datetime
import errno
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = ((MAX_PDF_BYTES + 2) // 3) * 4 + 8192
MAX_RESPONSE_BYTES = 1024 * 1024
WALL_TIMEOUT_SECONDS = 35
ADDRESS_SPACE_BYTES = 384 * 1024 * 1024


class ForecastMediaWorkerError(RuntimeError):
    """Sanitized infrastructure/resource error; never contains source or stderr."""


def _safe_environment(private_directory: str) -> dict[str, str]:
    # Do not copy os.environ: API/provider keys, proxy credentials, Python import
    # hooks and arbitrary native-library settings must not reach the media child.
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": private_directory, "TMPDIR": private_directory,
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1", "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "ORT_DISABLE_TELEMETRY": "1",
    }


def _child_command() -> list[str]:
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child"]


def _kill_group(process) -> None:
    # Kill even after the direct child exits: an OCR descendant may still retain
    # pipes or temporary files. start_new_session gives this job its own group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _check_memory_guard(memory_guard, phase):
    if memory_guard is None:
        return
    try:
        permitted = memory_guard(phase) is True
    except Exception:
        permitted = False
    if not permitted:
        raise ForecastMediaWorkerError("Local forecast review has insufficient verified service memory headroom. Retry later or use manual review.")


def _run_worker_process(request: bytes, timeout_seconds: float, *, memory_guard=None) -> tuple[bytes, float]:
    if sys.platform != "linux" or not hasattr(os, "killpg"):
        raise ForecastMediaWorkerError("Local isolated review is unavailable on this host; use the native PDF or manual review.")
    _check_memory_guard(memory_guard, "admission")
    started = time.monotonic()
    deadline = started + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="forecast-review-") as directory:
        os.chmod(directory, 0o700)
        # Anonymous/private request input avoids command-line data and a pipe
        # writer thread. All OCR temp files use this disposable private directory.
        with tempfile.TemporaryFile(mode="w+b", dir=directory) as source:
            source.write(request)
            source.seek(0)
            process = None
            try:
                process = subprocess.Popen(_child_command(), stdin=source, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, cwd=directory,
                                           env=_safe_environment(directory), close_fds=True,
                                           start_new_session=True)
                chunks, total = [], 0
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        _check_memory_guard(memory_guard, "running")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ForecastMediaWorkerError("Local forecast review reached its time limit. Resend a clearer report or use manual review.")
                        if not selector.select(min(remaining, 0.05) if memory_guard is not None else remaining):
                            if memory_guard is not None:
                                continue
                            raise ForecastMediaWorkerError("Local forecast review reached its time limit. Resend a clearer report or use manual review.")
                        chunk = os.read(process.stdout.fileno(), min(65536, MAX_RESPONSE_BYTES + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise ForecastMediaWorkerError("Local forecast review exceeded its response limit. Resend a simpler report or use manual review.")
                        chunks.append(chunk)
                # A native child can close stdout before exiting. Keep the
                # service guard live during that final wait too, not only while
                # the protocol pipe is open.
                while True:
                    _check_memory_guard(memory_guard, "running")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ForecastMediaWorkerError("Local forecast review reached its time limit. Resend a clearer report or use manual review.")
                    try:
                        code = process.wait(timeout=min(remaining, 0.05) if memory_guard is not None else remaining)
                        break
                    except subprocess.TimeoutExpired:
                        if memory_guard is None:
                            raise ForecastMediaWorkerError("Local forecast review reached its time limit. Resend a clearer report or use manual review.") from None
                if code != 0:
                    raise ForecastMediaWorkerError("Local forecast review could not finish within its safety limits. Resend the native PDF or use manual review.")
                _check_memory_guard(memory_guard, "running")
                return b"".join(chunks), time.monotonic() - started
            except OSError:
                raise ForecastMediaWorkerError("The isolated local review process is unavailable. Use the native PDF or manual review.") from None
            finally:
                if process is not None:
                    _kill_group(process)
                    if process.stdout is not None:
                        process.stdout.close()


def evaluate_forecast_media_isolated(
    data, mime_type, hotel_code, room_capacity, now, *,
    report_profile="canonical_v1", hotel_name=None, timeout_seconds=WALL_TIMEOUT_SECONDS, memory_guard=None,
):
    """Return the assessment dict plus non-sensitive ``isolation`` measurements.

    No enrollment, settings, roster, provider or durable storage is changed.
    Unsupported/resource failures are explicit errors, never automatic retries.
    ``timeout_seconds`` can shorten, but cannot raise, the hard wall deadline.
    Optional ``memory_guard(phase)`` must return True for admission/running.
    It stays in the parent and is polled at most every 50ms while waiting. This
    watchdog cannot replace a hard aggregate cgroup limit or prevent every OOM.
    """
    if not isinstance(mime_type, str) or mime_type not in {"application/pdf", "image/png", "image/jpeg"} or not isinstance(data, bytes) or not data:
        raise ValueError("Supply a nonempty PDF, PNG or JPEG for local review.")
    if len(data) > (MAX_PDF_BYTES if mime_type == "application/pdf" else MAX_IMAGE_BYTES):
        raise ValueError("The report exceeds the PDF/photo byte limit.")
    if (not isinstance(hotel_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", hotel_code)
            or type(room_capacity) is not int or not 1 <= room_capacity <= 100_000
            or not isinstance(now, datetime) or not isinstance(report_profile, str)
            or report_profile not in {"canonical_v1", "hotel_daily_table_v1"}
            or (hotel_name is not None and (not isinstance(hotel_name, str) or not 1 <= len(hotel_name) <= 160))):
        raise ValueError("Explicit supported enrollment, saved capacity and reference time are required.")
    if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= WALL_TIMEOUT_SECONDS:
        raise ValueError("The local review deadline must be positive and at most 35 seconds.")
    if memory_guard is not None and not callable(memory_guard):
        raise ValueError("The optional memory guard must be callable.")
    _check_memory_guard(memory_guard, "admission")
    request = json.dumps({"data": base64.b64encode(data).decode("ascii"), "mime_type": mime_type,
                          "hotel_code": hotel_code, "room_capacity": room_capacity,
                          "now": now.isoformat(), "report_profile": report_profile,
                          "hotel_name": hotel_name}, allow_nan=False, separators=(",", ":")).encode()
    if len(request) > MAX_REQUEST_BYTES:
        raise ValueError("The local review request exceeds its size limit.")
    raw, elapsed = _run_worker_process(request, float(timeout_seconds), memory_guard=memory_guard)
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
            raise ValueError
        result = envelope["result"]
        measurements = envelope["resources"]
        if not isinstance(measurements, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ForecastMediaWorkerError("Local forecast review could not safely assess this report. Resend the native PDF or use manual review.") from None
    result["isolation"] = dict(measurements, wall_seconds=round(elapsed, 3))
    return result


def _install_network_denial() -> None:
    """Deny network syscalls in this Linux process and every OCR descendant."""
    import ctypes
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW for non-network work.
    if not context:
        raise RuntimeError("Network isolation unavailable")
    try:
        for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                     "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
                     "io_uring_setup", "io_uring_enter", "io_uring_register"):
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0 or library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError("Network isolation unavailable")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("Network isolation unavailable")
    finally:
        library.seccomp_release(context)


def _apply_child_limits() -> None:
    import resource
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    _install_network_denial()


def _post_exec_peak_rss_kib() -> int:
    """Linux mm high-water for this executable, not inherited fork rusage.

    getrusage(RUSAGE_SELF).ru_maxrss can retain the pre-exec parent's high-water.
    VmHWM belongs to the current post-exec address space. Do not invent a number
    if this Linux measurement is unavailable.
    """
    status = Path("/proc/self/status").read_text(encoding="ascii")
    match = re.search(r"^VmHWM:[ \t]+([0-9]+)[ \t]+kB$", status, re.M)
    if not match or int(match.group(1)) <= 0:
        raise RuntimeError("Post-exec memory measurement unavailable")
    return int(match.group(1))


def _child_main() -> int:
    # Preserve a private protocol descriptor; suppress all library stdout/stderr,
    # including native C-level diagnostics, without retaining sensitive logs.
    output_fd = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        _apply_child_limits()
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError
        request = json.loads(raw)
        # The bounded transport envelope is not source evidence. Release its
        # base64/JSON copy before native rendering/OCR allocates image buffers.
        del raw
        content = base64.b64decode(request.pop("data"), validate=True)
        request["now"] = datetime.fromisoformat(request["now"])
        # Current PyMuPDF distributions may auto-load an optional ONNX layout
        # model. It is unnecessary for raster rendering and can reserve >2 GiB.
        # Disable only these optional integrations in this isolated process.
        for module in ("pymupdf.layout", "pymupdf4llm", "onnxruntime"):
            sys.modules[module] = None
        # pytesseract optionally imports pandas for Output.DATAFRAME. This
        # worker exclusively supplies PIL images and requests Output.DICT;
        # loading the unused DataFrame adapter adds >150 MiB of virtual memory
        # here and can exhaust the unchanged AS cap after a scanned PDF render.
        # Keep NumPy available for rendering integrations; disable pandas only
        # in this disposable child, never in the manual app/parent process.
        sys.modules["pandas"] = None
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from services.forecast_media_review import evaluate_forecast_media
        result = evaluate_forecast_media(content, **request)
        import resource
        resources = {"network_denied": True, "address_space_limit_mib": ADDRESS_SPACE_BYTES // (1024 * 1024),
                     "worker_peak_rss_kib": _post_exec_peak_rss_kib(),
                     "worker_peak_rss_source": "proc_self_status_VmHWM",
                     # This observation can inherit pre-exec high-water and is
                     # neither precise OCR RSS nor simultaneous/aggregate usage.
                     "child_rusage_high_water_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                     "child_rusage_may_include_inherited_peak": True,
                     "child_metric_is_precise_ocr_peak": False,
                     "aggregate_memory_cap": False}
        encoded = json.dumps({"ok": True, "result": result, "resources": resources},
                             allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError
        offset = 0
        while offset < len(encoded):
            offset += os.write(output_fd, encoded[offset:])
        return 0
    except Exception:
        # Deliberately do not serialize exception messages or tracebacks.
        try:
            os.write(output_fd, b'{"ok":false}')
        except OSError:
            pass
        return 1
    finally:
        os.close(output_fd)


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit(2)
    raise SystemExit(_child_main())
