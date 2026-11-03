import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_startup


def write_seam_source(root: Path, text: str) -> Path:
    seam = root / "skills/slop-cleanup/references/structural-consolidation.md"
    seam.parent.mkdir(parents=True)
    seam.write_text(text)
    methods = root / "skills/repository-readiness/references"
    methods.mkdir(parents=True, exist_ok=True)
    (methods / "developer-navigation.md").write_text("# Navigation method\n")
    (methods / "storytelling-readability.md").write_text("# Readability method\n")
    (methods / "agentic-readiness.md").write_text("# Readiness\n\n## Check the seven capabilities\n")
    transport = root / "skills/pinboard-intake/references/codex-transport.md"
    transport.parent.mkdir(parents=True, exist_ok=True)
    transport.write_text("# Codex task transport\n")
    (root / "skills/pinboard/startup/parts/intake.md").write_text("# Intake\n")
    return seam


class StartupDocumentsTest(unittest.TestCase):
    def test_generation_selects_roles_and_fails_freshness_without_writing(self) -> None:  # noqa: PLR0915 - one generation/freshness/repair journey
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            skill = root / "skills/pinboard"
            parts = skill / "startup/parts"
            references = skill / "references"
            parts.mkdir(parents=True)
            references.mkdir()
            write_seam_source(
                root, "# Consolidation\n\n## Choose explicit test seams\n\nseam-rule\n\n## Other\n\nexcluded\n"
            )
            common = parts / "common.md"
            common.write_text("# Common\n\nshared é\n\n[local](common.md#common)\n[whole](common.md)\n")
            (parts / "intake.md").write_text(
                "# Intake\n\nproposal-rule\n<!-- roles: coordinator -->\n\nstart-rule\n"
                "<!-- roles: worker -->\n\nnested-rule\n"
            )
            for role in generate_startup.ROLES:
                intake = "[proposal](intake.md#intake)\n" if role != "reviewer" else ""
                (parts / f"{role}.md").write_text(f"# Role\n\nrole-{role}\n{intake}")
            for phase in generate_startup.PHASES:
                content = "optional-health" if phase == "engineering-health-baseline" else "phase-only"
                (references / f"{phase}.md").write_text(f"# {phase}\n\n{content}\n")
            shared = references / "runtime-adapters.md"
            shared.write_text(
                "<!-- roles: all -->\n\n# Runtime contract\n\nuniversal\n"
                "<!-- roles: coordinator -->\n\ncoordinator-only\n"
                "<!-- roles: worker -->\n\nworker-only\n"
                "<!-- roles: reviewer -->\n\nreviewer-only\n"
                "<!-- roles: launched -->\n\nlaunched-only\n"
                "<!-- roles: coordinator worker -->\n\nsource-setup\n"
            )
            for runtime in generate_startup.RUNTIMES:
                (references / f"runtime-{runtime}.md").write_text(f"# {runtime}\n\nruntime-{runtime}\n")
            script = root / "scripts/generate_startup.py"
            with (
                patch.object(generate_startup, "__file__", str(script)),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(0, generate_startup.main([]))
                self.assertEqual(0, generate_startup.main(["--check"]))
                originals = {path: path.read_bytes() for path in (skill / "startup").glob("*-*.md")}
                self.assertEqual(9, len(originals))
                for path, data in originals.items():
                    self.assertEqual(data.rstrip() + b"\n", data)
                    role, runtime = path.stem.split("-", 1)
                    text = data.decode()
                    self.assertIn(f"role-{role}", text)
                    self.assertIn(f"runtime-{runtime}", text)
                    self.assertIn("seam-rule", text)
                    self.assertNotIn("excluded", text)
                    for sibling in {"coordinator", "worker", "reviewer"} - {role}:
                        self.assertNotIn(f"role-{sibling}", text)
                        self.assertNotIn(f"{sibling}-only", text)
                    self.assertEqual(role == "coordinator", "phase-only" in text)
                    self.assertEqual(role != "reviewer", "optional-health" in text)
                    self.assertEqual(role != "coordinator", "launched-only" in text)
                    self.assertEqual(role != "reviewer", "source-setup" in text)
                    self.assertEqual(role != "reviewer", "proposal-rule" in text)
                    self.assertEqual(role == "coordinator", "start-rule" in text)
                    self.assertEqual(role == "worker", "nested-rule" in text)
                    self.assertEqual(role != "reviewer", "[proposal](#intake)" in text)
                    self.assertNotIn("<!-- roles:", text)
                    self.assertIn("[local](#common)", text)
                common.write_text(common.read_text() + "\nchanged shared source\n")
                self.assertEqual(1, generate_startup.main(["--check"]))
                self.assertEqual(originals, {path: path.read_bytes() for path in originals})
                self.assertEqual(0, generate_startup.main([]))
                self.assertEqual((), generate_startup.generate(root, True))
                paths = list(originals)
                paths[0].write_bytes(b"stale")
                paths[1].unlink()
                self.assertEqual({paths[0], paths[1]}, set(generate_startup.generate(root, True)))
                self.assertEqual(b"stale", paths[0].read_bytes())
                self.assertFalse(paths[1].exists())
                self.assertEqual({paths[0], paths[1]}, set(generate_startup.generate(root, False)))
                self.assertEqual((), generate_startup.generate(root, True))
                self.assertIn(paths[0].relative_to(root).as_posix(), output.getvalue())

    def test_invalid_scope_and_missing_inline_target_reject_composition(self) -> None:
        with self.assertRaises(ValueError):
            generate_startup.scoped("<!-- roles: unknown -->\ncontent", "worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parts = root / "skills/pinboard/startup/parts"
            refs = root / "skills/pinboard/references"
            parts.mkdir(parents=True)
            refs.mkdir()
            (refs / "engineering-health-baseline.md").write_text("# Optional health\n")
            seam = write_seam_source(root, "# Consolidation\n\n## Choose explicit test seams\n\nseam-rule\n")
            (parts / "common.md").write_text("# Common\n\n[absent](#absent)\n[missing](worker.md#missing)\n")
            (parts / "worker.md").write_text(
                "# Worker\n\n[web](https://example.org)\n[project](../../../../AGENTS.md)\n"
            )
            (refs / "runtime-adapters.md").write_text("# Runtime\n")
            (refs / "runtime-codex.md").write_text("# Codex\n")
            with self.assertRaisesRegex(ValueError, "#absent"):
                generate_startup.compose(root, "worker", "codex")
            seam.unlink()
            with self.assertRaises(ValueError):
                generate_startup.compose(root, "worker", "codex")

    def test_links_preserve_source_identity_across_duplicate_and_omitted_headings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parts = root / "skills/pinboard/startup/parts"
            refs = root / "skills/pinboard/references"
            parts.mkdir(parents=True)
            refs.mkdir()
            write_seam_source(root, "# Consolidation\n\n## Choose explicit test seams\n")
            (refs / "engineering-health-baseline.md").write_text("# Health\n")
            (parts / "common.md").write_text("# Common\n")
            worker = parts / "worker.md"
            worker.write_text("# Worker\n\n[target](../../references/runtime-codex.md#operations)\n")
            (refs / "runtime-adapters.md").write_text("# Shared\n\n## Operations\nfirst\n")
            (refs / "runtime-codex.md").write_text("# Codex\n\n## Operations\nsecond\n[own](#operations)\n")
            text = generate_startup.compose(root, "worker", "codex")
            self.assertIn("[target](#operations-1)", text)
            self.assertIn("[own](#operations-1)", text)
            worker.write_text("# Worker\n\n[target](../../references/runtime-antigravity.md#operations)\n")
            with self.assertRaisesRegex(ValueError, "runtime-antigravity.md#operations"):
                generate_startup.compose(root, "worker", "codex")
            worker.write_text("# Worker\n\n[target](../../references/runtime-codex.md#operations)\n")
            (refs / "runtime-codex.md").write_text("# Codex\n<!-- roles: coordinator -->\n## Operations\nexcluded\n")
            with self.assertRaisesRegex(ValueError, "runtime-codex.md#operations"):
                generate_startup.compose(root, "worker", "codex")
