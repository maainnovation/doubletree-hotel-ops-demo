"""Real subprocess boundaries; no provider, external network or real hotel data."""
from datetime import datetime, timezone
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from services import forecast_media_worker as worker
from test_whatsapp_forecast import NOW, pdf_bytes, report_lines


def assess(data=None, mime="application/pdf", **kwargs):
    return worker.evaluate_forecast_media_isolated(
        pdf_bytes() if data is None else data, mime, "SYNTHETIC-HOTEL", 200, NOW, **kwargs)


def fake_command(monkeypatch, code):
    monkeypatch.setattr(worker, "_child_command", lambda: [sys.executable, "-I", "-c", code])


def track_private_directories(monkeypatch):
    created = []
    original = worker.tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(Path(result.name))
        return result
    monkeypatch.setattr(worker.tempfile, "TemporaryDirectory", temporary)
    return created


def synthetic_image(size=(2800, 1200)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 32)
    for index, line in enumerate(report_lines()):
        draw.text((40, 40 + index * 95), line, fill="black", font=font)
    return image


def test_real_native_worker_preserves_eligible_result_and_reports_measured_limits():
    result = assess()
    assert result["method"] == "native_pdf" and result["automatic_roster_allowed"]
    assert len(result["rows"]) == 7
    assert result["isolation"]["network_denied"]
    assert result["isolation"]["address_space_limit_mib"] == 384
    assert 0 < result["isolation"]["worker_peak_rss_kib"] < 384 * 1024
    assert result["isolation"]["aggregate_memory_cap"] is False
    assert result["isolation"]["worker_peak_rss_source"] == "proc_self_status_VmHWM"
    assert result["isolation"]["child_rusage_may_include_inherited_peak"] is True
    assert result["isolation"]["child_metric_is_precise_ocr_peak"] is False
    assert "ocr_child_peak_rss_kib" not in result["isolation"]


@pytest.mark.parametrize("kind", ["photo", "scan", "12mp_photo"])
def test_real_synthetic_photo_and_scan_ocr_stay_review_only_with_measured_usage(kind):
    scanned = kind == "scan"
    image = synthetic_image((4000, 3000) if kind == "12mp_photo" else (2800, 1200))
    out = BytesIO()
    if scanned:
        pdf = canvas.Canvas(out, pagesize=image.size)
        pdf.drawImage(ImageReader(image), 0, 0, width=image.width, height=image.height)
        pdf.save()
    else:
        image.save(out, format="PNG")
    result = assess(out.getvalue(), "application/pdf" if scanned else "image/png")
    assert result["requires_review"] and not result["automatic_roster_allowed"]
    assert len(result["evidence"]) == 2
    assert any(item["numeric_tokens"] > 0 for item in result["evidence"]), result
    assert result["isolation"]["wall_seconds"] < worker.WALL_TIMEOUT_SECONDS
    assert result["isolation"]["worker_peak_rss_kib"] < 384 * 1024


def test_scan_retains_headroom_without_optional_dataframe_import(monkeypatch):
    # This real synthetic scan used to exhaust the unchanged 384 MiB AS cap
    # inside pytesseract's unnecessary pandas import with only 32 MiB extra
    # retained allocation. Keep that pressure as a deterministic regression,
    # rather than relying on allocator/import-layout variation to reproduce it.
    root = str(Path(worker.__file__).resolve().parents[1])
    code = f'''
import sys
sys.path.insert(0, {root!r})
from services import forecast_media_worker as w
original_limits = w._apply_child_limits
original_peak = w._post_exec_peak_rss_kib
def limited_with_pressure():
    original_limits()
    w.synthetic_pressure = bytearray(32 * 1024 * 1024)
def checked_peak():
    assert 'pandas' in sys.modules and sys.modules['pandas'] is None
    return original_peak()
w._apply_child_limits = limited_with_pressure
w._post_exec_peak_rss_kib = checked_peak
raise SystemExit(w._child_main())
'''
    fake_command(monkeypatch, code)
    image = synthetic_image()
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=image.size)
    pdf.drawImage(ImageReader(image), 0, 0, width=image.width, height=image.height)
    pdf.save()
    result = assess(out.getvalue())
    assert result["requires_review"] and not result["automatic_roster_allowed"]
    assert [item["numeric_tokens"] for item in result["evidence"]] == [56, 56]
    assert [item["complete_cells"] for item in result["evidence"]] == [49, 49]
    from test_whatsapp_forecast import admit
    assert result["rows"] == admit()
    assert result["isolation"]["address_space_limit_mib"] == 384
    assert 0 < result["isolation"]["worker_peak_rss_kib"] < 384 * 1024


def test_private_temps_are_removed_after_success(monkeypatch):
    directories = track_private_directories(monkeypatch)
    assess()
    assert directories and all(not path.exists() for path in directories)


def test_timeout_kills_process_group_including_ocr_descendant_and_cleans_temps(monkeypatch, tmp_path):
    marker = tmp_path / "descendant.txt"
    code = ("import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']); "
            f"open({str(marker)!r},'w').write(str(child.pid)); "
            "time.sleep(90)")
    fake_command(monkeypatch, code)
    directories = track_private_directories(monkeypatch)
    started = time.monotonic()
    with pytest.raises(worker.ForecastMediaWorkerError, match="time limit"):
        assess(timeout_seconds=0.3)
    assert time.monotonic() - started < 3
    assert all(not path.exists() for path in directories)
    pid = int(marker.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_output_overflow_is_killed_before_unbounded_capture_and_temp_cleanup(monkeypatch):
    fake_command(monkeypatch, "import os,time;os.write(1,b'x'*1100000);time.sleep(90)")
    directories = track_private_directories(monkeypatch)
    with pytest.raises(worker.ForecastMediaWorkerError, match="response limit"):
        assess(timeout_seconds=2)
    assert directories and all(not path.exists() for path in directories)


def test_nonzero_failure_and_stderr_are_sanitized_and_cleaned(monkeypatch):
    fake_command(monkeypatch, "import sys;print('SOURCE SECRET',file=sys.stderr);sys.exit(23)")
    directories = track_private_directories(monkeypatch)
    with pytest.raises(worker.ForecastMediaWorkerError) as error:
        assess()
    assert "SOURCE" not in str(error.value) and "SECRET" not in str(error.value)
    assert all(not path.exists() for path in directories)


def test_bad_output_is_never_returned_as_a_result(monkeypatch):
    fake_command(monkeypatch, "print('SOURCE SECRET')")
    with pytest.raises(worker.ForecastMediaWorkerError) as error:
        assess()
    assert "SOURCE" not in str(error.value)


def test_child_environment_never_inherits_credentials_provider_or_proxy_config(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "SECRET")
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET")
    monkeypatch.setenv("META_APP_SECRET", "SECRET")
    monkeypatch.setenv("HTTPS_PROXY", "https://SECRET")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/imports")
    code = ("import os,json; print(json.dumps({'ok':True,'result':{'keys':sorted(os.environ)},'resources':{}}))")
    fake_command(monkeypatch, code)
    keys = assess()["keys"]
    assert not set(keys) & {"TWILIO_AUTH_TOKEN", "OPENAI_API_KEY", "META_APP_SECRET", "HTTPS_PROXY", "PYTHONPATH"}
    assert {"OPENBLAS_NUM_THREADS", "OMP_THREAD_LIMIT", "TMPDIR", "HOME"} <= set(keys)


def test_real_seccomp_denies_socket_creation_in_child_and_descendant(monkeypatch):
    root = str(Path(worker.__file__).resolve().parents[1])
    code = (f"import sys;sys.path.insert(0,{root!r}); "
            "from services.forecast_media_worker import _install_network_denial; "
            "_install_network_denial(); import socket,subprocess,json; "
            "\ntry: socket.socket(); denied=False\nexcept PermissionError: denied=True\n"
            "child=subprocess.run([sys.executable,'-c','import socket;socket.socket()'],capture_output=True); "
            "print(json.dumps({'ok':True,'result':{'denied':denied,'child_denied':child.returncode!=0},'resources':{}}))")
    fake_command(monkeypatch, code)
    result = assess()
    assert result["denied"] and result["child_denied"]


def test_address_space_limit_is_applied_before_media_decoding(monkeypatch):
    root = str(Path(worker.__file__).resolve().parents[1])
    code = (f"import sys;sys.path.insert(0,{root!r}); "
            "from services.forecast_media_worker import _apply_child_limits; "
            "_apply_child_limits(); import json,resource; "
            "\ntry: x=bytearray(600*1024*1024); blocked=False\nexcept MemoryError: blocked=True\n"
            "print(json.dumps({'ok':True,'result':{'blocked':blocked,'limit':resource.getrlimit(resource.RLIMIT_AS)[0]},'resources':{}}))")
    fake_command(monkeypatch, code)
    result = assess()
    assert result["blocked"] and result["limit"] == 384 * 1024 * 1024


def test_missing_linux_isolation_fails_closed_before_spawn(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "not-linux")
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Must not spawn without isolation"))
    with pytest.raises(worker.ForecastMediaWorkerError, match="unavailable"):
        assess()


@pytest.mark.parametrize("kwargs", [
    {"data": b""}, {"data": b"x" * (worker.MAX_PDF_BYTES + 1)},
    {"data": b"x" * (worker.MAX_IMAGE_BYTES + 1), "mime": "image/png"},
    {"mime": []}, {"report_profile": []}, {"report_profile": "auto"},
    {"timeout_seconds": 36}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
])
def test_invalid_or_oversized_input_never_spawns(monkeypatch, kwargs):
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Invalid input must not spawn"))
    with pytest.raises(ValueError):
        assess(**kwargs)


def test_missing_kernel_network_isolation_never_silently_runs_unprotected(monkeypatch):
    root = str(Path(worker.__file__).resolve().parents[1])
    code = (f"import sys;sys.path.insert(0,{root!r}); "
            "from services import forecast_media_worker as w; "
            "w._install_network_denial=lambda:(_ for _ in ()).throw(RuntimeError('SECRET FAILURE')); "
            "raise SystemExit(w._child_main())")
    fake_command(monkeypatch, code)
    with pytest.raises(worker.ForecastMediaWorkerError) as error:
        assess()
    assert "SECRET" not in str(error.value)


def test_post_exec_worker_peak_does_not_inherit_parent_memory_high_water(tmp_path):
    # A fresh helper bounds the allocation independently of an already-large
    # pytest process. Release the allocation before spawning the actual worker;
    # the parent's historical peak must not be attributed to its new executable.
    root = str(Path(worker.__file__).resolve().parents[1])
    code = f'''
import base64, gc, json, sys
from datetime import datetime
sys.path.insert(0, {root!r})
from services.forecast_media_worker import evaluate_forecast_media_isolated, _post_exec_peak_rss_kib
request = json.loads(sys.stdin.read())
padding = bytearray(b'x') * (96 * 1024 * 1024)
parent_peak = _post_exec_peak_rss_kib()
del padding
gc.collect()
result = evaluate_forecast_media_isolated(base64.b64decode(request['pdf']), 'application/pdf',
    'SYNTHETIC-HOTEL', 200, datetime.fromisoformat(request['now']), timeout_seconds=3)
print(json.dumps({{'parent_peak': parent_peak, 'worker': result['isolation']}}))
'''
    payload = json.dumps({"pdf": base64.b64encode(pdf_bytes()).decode(), "now": NOW.isoformat()}).encode()
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", code], input=payload,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=worker._safe_environment(str(tmp_path)), cwd=tmp_path, timeout=8)
    assert completed.returncode == 0
    measured = json.loads(completed.stdout)
    assert measured["parent_peak"] > 96 * 1024
    assert 0 < measured["worker"]["worker_peak_rss_kib"] < 384 * 1024
    assert measured["worker"]["worker_peak_rss_kib"] < measured["parent_peak"] - 16 * 1024
    assert measured["worker"]["worker_peak_rss_source"] == "proc_self_status_VmHWM"
