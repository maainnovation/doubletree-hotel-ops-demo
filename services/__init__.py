"""Hotel operation services; native dependency telemetry is never opted in."""
from .privacy import disable_dependency_telemetry

disable_dependency_telemetry()
