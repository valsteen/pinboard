"""Run the installed CLI entry with an injected target, for capture tests that need a separate process.

Tests start it as `python -m tests.cli_capture_support <CLI arguments>` from the repository root, either
directly or through a copied launcher whose `.venv/bin/pinboard` wrapper execs it, so the launcher's
exported path, the process's own descriptors and its signals are real. `PINBOARD_TEST_TARGET` selects
what the CLI target does instead of parsing its arguments:

- `status:<code>`: write one stdout and one stderr line, append `x` to `PINBOARD_TEST_EFFECT` when set,
  and return the code;
- `raise`: write partial output, then raise a chained exception that nothing catches;
- `system-exit-text`: exit through `SystemExit` with a text code;
- `large`: write 200000 bytes to stdout and 70000 to stderr;
- `signal:<NAME>`: write partial output, then send that signal to this process;
- `occupy:<path>`: create the selected capture destination and return 0, so publication finds it taken;
- `occupy-raise:<path>`: create the selected capture destination, then fail like `raise`;
- `settings-off:<path>`: write a project mode of off to the settings file at path and return 0.

`PINBOARD_TEST_PUBLICATION_SIGNAL=<NAME>` sends that signal while the record is being linked,
`PINBOARD_TEST_HANDOFF_SIGNAL=<NAME>` sends it once on entry to `CaptureSession.finish`, after the target
ended and before capture stops interrupting, and `PINBOARD_TEST_PRUNE_FAILURE=1` makes automatic
retention fail.
"""

import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files import contributor_traces
from pinboard.cli import entrypoint, invocation_capture


def _target(_arguments: tuple[str, ...]) -> int:
    match os.environ["PINBOARD_TEST_TARGET"].split(":", 1):
        case ["status", code]:
            print("target-stdout")
            print("target-stderr", file=sys.stderr)
            if (effect := os.environ.get("PINBOARD_TEST_EFFECT")) is not None:
                with Path(effect).open("a", encoding="utf-8") as marker:
                    marker.write("x")
            return int(code)
        case ["raise"]:
            print("partial", flush=True)
            try:
                raise KeyError("inner")
            except KeyError as error:
                raise RuntimeError("target failed") from error
        case ["system-exit-text"]:
            print("partial")
            sys.exit("text exit code")
        case ["large"]:
            sys.stdout.write("o" * 200_000)
            sys.stderr.write("e" * 70_000)
            return 3
        case ["signal", name]:
            print("partial")
            print("interrupted", file=sys.stderr)
            signal.raise_signal(signal.Signals[name])
            for _ in range(1000):
                pass
            print("signal-not-delivered")
            return 0
        case ["occupy", path]:
            print("target-output")
            Path(path).write_bytes(b"occupied")
            return 0
        case ["occupy-raise", path]:
            print("partial", flush=True)
            Path(path).write_bytes(b"occupied")
            raise RuntimeError("target failed")
        case ["settings-off", path]:
            Path(path).write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n', encoding="utf-8")
            return 0
        case unknown:
            raise AssertionError(f"Unknown capture test target: {unknown}")


def _link_with_signal(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    signal.raise_signal(signal.Signals[os.environ["PINBOARD_TEST_PUBLICATION_SIGNAL"]])
    for _ in range(1000):
        pass
    _real_link(source, destination)


def _finish_with_signal(session: invocation_capture.CaptureSession, outcome: int | BaseException) -> int:
    if not _handoff_signalled:
        _handoff_signalled.append(True)
        signal.raise_signal(signal.Signals[os.environ["PINBOARD_TEST_HANDOFF_SIGNAL"]])
        for _ in range(1000):
            pass
    return _real_finish(session, outcome)


def _failing_prune(_directory: Path) -> None:
    raise PermissionError("retention denied")


_real_link = os.link
_real_finish = invocation_capture.CaptureSession.finish
_handoff_signalled: list[bool] = []

if __name__ == "__main__":
    with patch.object(entrypoint, "_run_cli", side_effect=_target):
        if "PINBOARD_TEST_PUBLICATION_SIGNAL" in os.environ:
            patch.object(os, "link", side_effect=_link_with_signal).start()
        if "PINBOARD_TEST_HANDOFF_SIGNAL" in os.environ:
            patch.object(invocation_capture.CaptureSession, "finish", _finish_with_signal).start()
        if os.environ.get("PINBOARD_TEST_PRUNE_FAILURE") == "1":
            patch.object(contributor_traces, "prune_traces", side_effect=_failing_prune).start()
        sys.exit(entrypoint.main())
