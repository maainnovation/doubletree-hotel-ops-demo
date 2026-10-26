"""No provider, live service, Docker engine or real media is used by these tests."""
from pathlib import Path
from datetime import datetime, timezone
import subprocess

import pytest

from scripts import measure_review_runtime as resources


def write_cgroup(root, *, current=200, maximum="536870912", cpu="50000 100000", oom=0, kill=0):
    for name, value in {
        "memory.current": str(current * resources.MIB), "memory.max": maximum,
        "memory.peak": str(current * resources.MIB), "cpu.max": cpu,
        "memory.events": f"low 0\nhigh 0\nmax 0\noom {oom}\noom_kill {kill}\n",
    }.items():
        (root / name).write_text(value)


def passing_report():
    state = {"current": 200 * resources.MIB, "maximum": 512 * resources.MIB,
             "cpu_cores": 0.5, "oom": 0, "oom_kill": 0, "historical_peak": 400 * resources.MIB}
    return {"mode": "attached-disabled", "service_processes_present": True,
            "service_continuity_verified": True,
            "cgroup_before": dict(state), "cgroup_after": dict(state),
            "sampled_cgroup_peak_bytes": 400 * resources.MIB,
            "minimum_free_temp_bytes": 200 * resources.MIB,
            "temporary_preflight_passed": True,
            "cases": [{"name": name, "completed": True} for name in resources.CASES]}


def test_finite_cgroup_snapshot_and_admission_vs_running_thresholds(tmp_path):
    write_cgroup(tmp_path)
    state = resources.cgroup_snapshot(tmp_path)
    assert state["maximum"] == 512 * resources.MIB and state["cpu_cores"] == 0.5
    guard = resources.CgroupMemoryGuard(tmp_path)
    assert guard("admission")
    write_cgroup(tmp_path, current=321)
    assert not guard("admission") and guard("running")
    write_cgroup(tmp_path, current=449)
    assert not guard("running")


@pytest.mark.parametrize('name,memory,cpu',[
    ('render_starter_512m',512,0.5),('render_standard_2g',2048,1.0),
])
def test_allowlisted_profiles_match_exact_host_and_keep_reserves(tmp_path,name,memory,cpu):
    options=dict(maximum=str(memory*resources.MIB),cpu=f'{int(cpu*100000)} 100000')
    write_cgroup(tmp_path,current=memory-192,**options)
    guard=resources.CgroupMemoryGuard(tmp_path,profile=name)
    assert guard('admission')
    write_cgroup(tmp_path,current=memory-191,**options)
    assert not guard('admission') and guard('running')
    write_cgroup(tmp_path,current=memory-64,**options)
    assert guard('running')
    write_cgroup(tmp_path,current=memory-63,**options)
    assert not guard('running')
    write_cgroup(tmp_path,current=memory-200,**(options|{'cpu':'25000 100000'}))
    assert not guard('admission')


@pytest.mark.parametrize('name',['','2048','render_standard_4g',' render_standard_2g',None,2048])
def test_unknown_resource_profiles_never_choose_custom_limits(name):
    with pytest.raises(ValueError):resources.resource_profile({'PILOT_REVIEW_RESOURCE_PROFILE':name})
    report=passing_report();report['resource_profile']=name
    assert resources.qualification(report)


def test_larger_host_requires_explicit_matching_profile(tmp_path,monkeypatch):
    monkeypatch.delenv('PILOT_REVIEW_RESOURCE_PROFILE',raising=False)
    write_cgroup(tmp_path,maximum=str(2048*resources.MIB),cpu='100000 100000')
    assert not resources.CgroupMemoryGuard(tmp_path)('admission')
    monkeypatch.setenv('PILOT_REVIEW_RESOURCE_PROFILE','render_standard_2g')
    assert resources.CgroupMemoryGuard(tmp_path)('admission')
    assert resources._safe_env(tmp_path)['PILOT_REVIEW_RESOURCE_PROFILE']=='render_standard_2g'
    report=passing_report();report['resource_profile']='render_standard_2g'
    for side in ('cgroup_before','cgroup_after'):
        report[side].update(maximum=2048*resources.MIB,cpu_cores=1)
    assert resources.qualification(report)==[]
    report['cgroup_after']['historical_peak']=1985*resources.MIB
    assert any('64 MiB' in reason for reason in resources.qualification(report))


def test_profile_does_not_expand_isolated_child_or_upload_limits():
    from services import forecast_media_worker as worker
    from services import forecast_media_review as media
    assert worker.ADDRESS_SPACE_BYTES==384*resources.MIB
    assert worker.WALL_TIMEOUT_SECONDS==35
    assert worker.MAX_PDF_BYTES==10*resources.MIB and worker.MAX_IMAGE_BYTES==5*resources.MIB
    assert media.MAX_SOURCE_IMAGE_PIXELS==12_000_000 and media.MAX_WORKING_PIXELS==4_000_000


@pytest.mark.parametrize("maximum", ["max", "1073741824", "0", "garbled"])
def test_guard_rejects_unbounded_wrong_or_invalid_container_limit(tmp_path, maximum):
    write_cgroup(tmp_path, maximum=maximum)
    assert not resources.CgroupMemoryGuard(tmp_path)("admission")


def test_guard_rejects_missing_evidence_and_oom_increments(tmp_path):
    assert not resources.CgroupMemoryGuard(tmp_path)("admission")
    write_cgroup(tmp_path)
    guard = resources.CgroupMemoryGuard(tmp_path)
    assert not guard("running") and guard("admission")
    write_cgroup(tmp_path, oom=1)
    assert not guard("running")
    (tmp_path / "memory.events").write_text("low 0\n")
    assert resources.cgroup_snapshot(tmp_path) is None


def test_aggregate_gate_never_substitutes_process_or_child_rss():
    report = passing_report()
    assert resources.qualification(report) == []
    report.update(cgroup_before=None, cgroup_after=None, diagnostic_process_tree_rss_peak_bytes=1)
    assert any("cgroup" in reason for reason in resources.qualification(report))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(mode="local"),
    lambda r: r.update(service_processes_present=False),
    lambda r: r.update(service_continuity_verified=False),
    lambda r: r["cgroup_before"].update(maximum=1024 * resources.MIB),
    lambda r: r["cgroup_before"].update(cpu_cores=1),
    lambda r: r["cgroup_after"].update(cpu_cores=1),
    lambda r: r["cgroup_after"].update(oom=1),
    lambda r: r["cgroup_after"].update(oom_kill=1),
    lambda r: r.update(sampled_cgroup_peak_bytes=449 * resources.MIB),
    lambda r: r["cgroup_after"].update(historical_peak=449 * resources.MIB),
    lambda r: r["cgroup_before"].update(historical_peak=None),
    lambda r: r["cgroup_after"].update(historical_peak=None),
    lambda r: r["cgroup_after"].update(historical_peak=399 * resources.MIB),
    lambda r: r.update(minimum_free_temp_bytes=127 * resources.MIB),
    lambda r: r.update(temporary_preflight_passed=False),
    lambda r: r["cases"].pop(),
    lambda r: r["cases"][0].update(completed=False),
])
def test_each_missing_or_unsafe_resource_fact_blocks_stage_gate(mutate):
    report = passing_report()
    mutate(report)
    assert resources.qualification(report)


def test_process_detection_requires_exact_module_arguments(tmp_path):
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "cgroup").write_text("0::/synthetic\n")
    for pid, command in (("1", b"python\0-m\0uvicorn\0api.twilio_review_pilot:app\0"),
                         ("2", b"python\0-m\0scripts.twilio_review_pilot_worker\0")):
        directory = tmp_path / pid
        directory.mkdir()
        (directory / "cmdline").write_bytes(command)
        (directory / "cgroup").write_text("0::/synthetic\n")
        (directory / "stat").write_text(f"{pid} (python) " + " ".join(["0"] * 19 + ["123"]))
    assert resources.disabled_service_processes_present(tmp_path)
    identity = resources.staged_service_identity(tmp_path)
    (tmp_path / "2" / "stat").write_text("2 (python) " + " ".join(["0"] * 19 + ["124"]))
    assert resources.staged_service_identity(tmp_path) != identity
    (tmp_path / "2" / "cgroup").write_text("0::/different\n")
    assert not resources.disabled_service_processes_present(tmp_path)
    (tmp_path / "2" / "cgroup").write_text("0::/synthetic\n")
    (tmp_path / "2" / "cmdline").write_bytes(b"python\0-c\0scripts.twilio_review_pilot_worker is only text\0")
    assert not resources.disabled_service_processes_present(tmp_path)


def test_process_tree_falls_back_to_ppid_when_kernel_children_files_are_missing(tmp_path):
    for pid, parent in ((5, 1), (6, 5), (7, 6), (8, 1)):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} (synthetic (child)) S {parent} 0 0 0")
    assert resources.process_tree(5, tmp_path) == {5, 6, 7}


def test_probe_children_have_disabled_minimal_environment_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "do-not-copy")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-copy")
    monkeypatch.setenv("REVIEW_PILOT_ENABLED", "1")
    env = resources._safe_env(tmp_path)
    assert "TWILIO_AUTH_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env["PILOT_REVIEW_ENABLED"] == env["REVIEW_PILOT_ENABLED"] == env["PILOT_ENABLED"] == "0"


def test_probe_requires_explicit_opt_in_and_disabled_state(monkeypatch):
    monkeypatch.setattr(resources, "run_probe", lambda *a, **k: pytest.fail("Must not run"))
    with pytest.raises(SystemExit):
        resources.main([])


@pytest.mark.parametrize("flag", ["PILOT_ENABLED", "PILOT_REVIEW_ENABLED", "REVIEW_PILOT_ENABLED"])
def test_enabled_service_refuses_probe_before_creating_files(monkeypatch, flag):
    monkeypatch.setenv(flag, "1")
    monkeypatch.setattr(resources.tempfile, "TemporaryDirectory", lambda *a, **k: pytest.fail("Must not create files"))
    with pytest.raises(ValueError, match="Disable"):
        resources.run_probe("local")


def test_low_disk_preflight_does_not_generate_fixtures_or_spawn(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setenv("PILOT_ENABLED", "0")
    monkeypatch.setenv("REVIEW_PILOT_ENABLED", "0")
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=200 * resources.MIB))
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *a, **k: pytest.fail("Must not spawn under low disk"))
    report = resources.run_probe("local", temp_parent=tmp_path)
    assert report["cases"] == [] and not report["temporary_preflight_passed"]
    assert not report["measured_stage_gate_passed"]


def test_new_image_is_nonroot_disabled_and_scoped_without_changing_rollback():
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile.review-pilot").read_text()
    assert "USER 1000:1000" in docker and "PILOT_REVIEW_ENABLED=0" in docker
    assert "tesseract-ocr-eng libseccomp2" in docker and "chmod -R a-w /app" in docker
    assert "COPY . " not in docker and "COPY .\n" not in docker
    assert 'CMD ["python", "-m", "scripts.run_twilio_review_pilot"]' in docker
    pins = (root / "requirements-review-pilot.txt").read_text()
    assert "PyMuPDF==2.0" in pins and "pytesseract==0.3.13" in pins
    assert "scripts.run_twilio_pilot" in (root / "Dockerfile.pilot").read_text()


def test_input_matrix_and_parallel_upload_buffer_are_at_admitted_limits():
    assert len(resources.CASES) == 4
    assert resources.UPLOAD_BUFFER_BYTES == 20 * resources.MIB
    assert resources.ADMISSION_HEADROOM_BYTES == 192 * resources.MIB
    assert resources.SAFETY_MARGIN_BYTES == 64 * resources.MIB


def test_incomplete_or_unsafe_extraction_is_not_counted_as_supported_resource_case():
    ocr = {"rows": 7, "automatic_allowed": False, "review_only": True,
           "complete_cells": [49, 49], "numeric_tokens": [56, 56]}
    assert resources.supported_case("photo_png_5mib", ocr)
    assert not resources.supported_case("scan_pdf_10mib", dict(ocr, rows=0))
    assert not resources.supported_case("photo_jpeg_5mib", dict(ocr, complete_cells=[42, 49]))
    assert not resources.supported_case("photo_png_5mib", dict(ocr, automatic_allowed=True))
    assert resources.supported_case("native_pdf_10mib", {"rows": 7, "automatic_allowed": True, "review_only": False})


def test_real_max_byte_and_12mp_fixtures_preserve_all_cells_without_automatic_ocr(tmp_path):
    from services.forecast_media_worker import evaluate_forecast_media_isolated
    from PIL import Image
    now = datetime.now(timezone.utc)
    generated = subprocess.run(resources._command("--generate", str(tmp_path), now.isoformat()),
                               env=resources._safe_env(tmp_path), cwd=tmp_path,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    assert generated.returncode == 0
    for case in resources.CASES:
        path = tmp_path / case
        assert path.stat().st_size == (10 if "pdf" in case else 5) * resources.MIB
        if "photo" in case:
            with Image.open(path) as image:
                assert image.size == (4000, 3000)
        mime = "application/pdf" if "pdf" in case else "image/png" if "png" in case else "image/jpeg"
        result = evaluate_forecast_media_isolated(path.read_bytes(), mime, "RESOURCE-SYNTHETIC", 200, now)
        assessment = {"rows": len(result["rows"]), "automatic_allowed": result["automatic_roster_allowed"],
                      "review_only": result["requires_review"],
                      "complete_cells": [item["complete_cells"] for item in result.get("evidence", [])],
                      "numeric_tokens": [item["numeric_tokens"] for item in result.get("evidence", [])]}
        assert resources.supported_case(case, assessment), case
        assert [row["forecast_occupied"] for row in result["rows"]] == [0, 80, 100, 120, 140, 160, 180]
