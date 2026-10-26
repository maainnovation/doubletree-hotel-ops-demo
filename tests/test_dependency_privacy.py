"""Pre-import opt-out; never enable ONNX or open a telemetry connection."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from services.privacy import disable_dependency_telemetry
from services.forecast_media_worker import _safe_environment


def test_forces_documented_opt_out_over_inherited_opt_in(monkeypatch):
    monkeypatch.delitem(sys.modules,'onnxruntime',raising=False)
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY','0')
    disable_dependency_telemetry()
    assert os.environ['ORT_DISABLE_TELEMETRY']=='1'


def test_already_initialized_unprotected_native_runtime_must_restart(monkeypatch):
    monkeypatch.setitem(sys.modules,'onnxruntime',object())
    monkeypatch.delenv('ORT_DISABLE_TELEMETRY',raising=False)
    with pytest.raises(RuntimeError,match='Restart'):
        disable_dependency_telemetry()


def test_sanitized_child_has_fixed_opt_out_not_inherited_opt_in(tmp_path,monkeypatch):
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY','0')
    assert _safe_environment(str(tmp_path))['ORT_DISABLE_TELEMETRY']=='1'


def test_docker_disables_telemetry_before_install_and_import():
    text=Path('Dockerfile.review-pilot').read_text()
    assert text.index('ORT_DISABLE_TELEMETRY=1')<text.index('RUN apt-get')


@pytest.mark.parametrize('module',['services','scripts','scripts.forecast_format_samples'])
def test_fresh_package_entrypoints_opt_out_before_optional_native_import(module):
    # The command's initial env is already opt-out; reset inside Python before
    # importing these entrypoints to prove their own startup fence.
    code=f"import os;os.environ['ORT_DISABLE_TELEMETRY']='0';import {module};assert os.environ['ORT_DISABLE_TELEMETRY']=='1'"
    result=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,ORT_DISABLE_TELEMETRY='1'),
        capture_output=True,timeout=20)
    assert result.returncode==0
