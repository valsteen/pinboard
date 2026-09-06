import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherTest(unittest.TestCase):
    def test_ready_environment_bypasses_uv_and_source_falls_back_to_locked_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            launcher = root / "scripts" / "pinboard"
            shutil.copyfile(Path(__file__).parents[1] / "scripts" / "pinboard", launcher)
            executable = root / ".venv" / "bin" / "pinboard"
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf "environment:%s\\n" "$*"\n', encoding="utf-8")
            executable.chmod(0o755)
            uv = root / "uv"
            uv.write_text('#!/bin/sh\nprintf "uv:%s\\n" "$*"\n', encoding="utf-8")
            uv.chmod(0o755)
            environment = {**os.environ, "PATH": f"{root}:/usr/bin:/bin", "UV_CACHE_DIR": "/dev/null/cache"}
            installed = subprocess.run(
                ["/bin/sh", str(launcher), "status", "--json"],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual("environment:status --json\n", installed.stdout)
            self.assertEqual("", installed.stderr)
            executable.unlink()
            source = subprocess.run(
                ["/bin/sh", str(launcher), "status"], env=environment, capture_output=True, text=True, check=True
            )
            self.assertEqual(f"uv:run --isolated --locked --no-dev --project {root} pinboard status\n", source.stdout)


if __name__ == "__main__":
    unittest.main()
