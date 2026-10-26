"""Disable optional dependency telemetry before any native library initializes.

ONNX Runtime's documented Linux 1DS switch must precede import; its later API
cannot undo initialization events. No endpoint/proxy manipulation is used.
https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md
"""
import os
import sys


def disable_dependency_telemetry():
    if sys.modules.get('onnxruntime') is not None and os.environ.get('ORT_DISABLE_TELEMETRY') != '1':
        raise RuntimeError('Restart the process with dependency telemetry disabled before native imports.')
    # Deliberately override an inherited opt-in: this application has no consent
    # to export optional OCR-library telemetry.
    os.environ['ORT_DISABLE_TELEMETRY']='1'
