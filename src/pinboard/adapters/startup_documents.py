"""Stable plugin-relative startup selection; no filesystem or authority effects."""

from typing import Literal

from pinboard.application.dispatch_models import NativeRuntime

type StartupRole = Literal["coordinator", "worker", "reviewer"]


def reference(role: StartupRole, runtime: NativeRuntime) -> str:
    return f"skills/pinboard/startup/{role}-{runtime}.md"
