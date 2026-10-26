"""Disable dependency telemetry before pytest imports any report fixtures."""
from services.privacy import disable_dependency_telemetry

# Also set this in the test command environment, before Python/plugin startup.
disable_dependency_telemetry()
