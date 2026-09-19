"""Exact occurrence certification and native nonexempt contribution contracts."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import msgspec
from scripts import check_duplication as policy


def occurrence(name: str, start: int, end: int, position: int) -> policy.Occurrence:
    return policy.Occurrence(
        name, start, end, policy.Location(start, 0, position), policy.Location(end, 1, position + 8)
    )


def pair(first: policy.Occurrence, second: policy.Occurrence, is_new: bool) -> policy.NativePair:
    return policy.NativePair(first, second, "python", "raw detector evidence", is_new, first.end - first.start + 1, 70)


def report(pairs: tuple[policy.NativePair, ...], source_lines: int) -> policy.NativeReport:
    lines = sum(p.firstFile.end - p.firstFile.start for p in pairs)
    statistics = policy.Statistics(len(pairs), lines, 70 * len(pairs), source_lines, 0, 0, 0.0, 0.0, 3, 200)
    return policy.NativeReport(pairs, policy.ReportStatistics("fixed-time", {"python": statistics}, statistics))


def certified_pair(native: policy.NativePair, source_root: Path) -> policy.ExceptionRecord:
    identity = policy.ReviewedPair(
        "required-shape",
        "python",
        native.tokens,
        native.lines,
        (
            policy.CertifiedOccurrence(native.firstFile, policy.occurrence_digest(source_root, native.firstFile)),
            policy.CertifiedOccurrence(native.secondFile, policy.occurrence_digest(source_root, native.secondFile)),
        ),
        ("fixture independent boundary",),
        "Fixture requires exact independent boundary shapes.",
    )
    return policy.ExceptionRecord(
        identity,
        policy.Certification("00000000-0000-4000-8000-000000000001", policy.reviewed_pair_digest(identity), "b" * 64),
    )


class DuplicationPolicyTests(unittest.TestCase):
    def test_certification_survives_unrelated_preceding_byte_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matched = b"a\nb\nc\nd\ne\nf\ng\nh\nx"

            def endpoint(name: str, position: int) -> policy.Occurrence:
                return policy.Occurrence(
                    name,
                    2,
                    10,
                    policy.Location(2, 0, position),
                    policy.Location(10, 1, position + len(matched)),
                )

            (root / "first.py").write_bytes(b"old\n" + matched)
            (root / "second.py").write_bytes(b"old\n" + matched)
            original = pair(endpoint("first.py", 4), endpoint("second.py", 4), False)
            exceptions = policy.Exceptions(
                "pinboard-duplication-exceptions/v1", "5.1.2", (certified_pair(original, root),)
            )

            prefix = b"longer prefix\n"
            (root / "first.py").write_bytes(prefix + matched)
            (root / "second.py").write_bytes(prefix + matched)
            shifted = pair(endpoint("first.py", len(prefix)), endpoint("second.py", len(prefix)), False)

            self.assertFalse(policy.evaluate(report((shifted,), 10000), exceptions, root).errors)

    def test_certification_changed_missing_and_unreviewed_occurrences_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.py").write_bytes(b"abcdefgh" * 4)
            (root / "second.py").write_bytes(b"abcdefgh" * 4)
            original = pair(occurrence("first.py", 1, 9, 0), occurrence("second.py", 1, 9, 0), False)
            accepted = certified_pair(original, root)
            exceptions = policy.Exceptions("pinboard-duplication-exceptions/v1", "5.1.2", (accepted,))
            self.assertFalse(policy.evaluate(report((original,), 10000), exceptions, root).errors)
            cases = (
                (msgspec.structs.replace(accepted, certification=None), (original,)),
                (
                    msgspec.structs.replace(
                        accepted, identity=msgspec.structs.replace(accepted.identity, rationale="Changed judgment")
                    ),
                    (original,),
                ),
                (accepted, ()),
                (accepted, (original, pair(original.firstFile, occurrence("second.py", 12, 20, 8), False))),
                (
                    accepted,
                    (original, pair(occurrence("first.py", 20, 28, 8), occurrence("second.py", 20, 28, 8), True)),
                ),
            )
            for record, pairs in cases:
                with self.subTest(record=record, pairs=pairs):
                    selected = policy.Exceptions(exceptions.schema, exceptions.detector_version, (record,))
                    self.assertTrue(policy.evaluate(report(pairs, 10000), selected, root).errors)
            (root / "first.py").write_bytes(b"CHANGED!" * 4)
            self.assertTrue(policy.evaluate(report((original,), 10000), exceptions, root).errors)
            (root / "first.py").unlink()
            self.assertTrue(policy.evaluate(report((original,), 10000), exceptions, root).errors)

    def test_overlapping_nonexempt_pairs_keep_native_contributions_and_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_bytes(b"abcdefgh" * 10)
            original = pair(occurrence("source.py", 1, 11, 0), occurrence("source.py", 20, 30, 8), False)
            overlap = pair(occurrence("source.py", 5, 14, 16), occurrence("source.py", 40, 49, 24), False)
            record = certified_pair(original, root)
            exceptions = policy.Exceptions("pinboard-duplication-exceptions/v1", "5.1.2", (record,))
            for denominator, fails in ((3000, False), (3001, False), (2999, True)):
                with self.subTest(source_lines=denominator):
                    result = policy.evaluate(report((original, overlap), denominator), exceptions, root)
                    self.assertEqual(9, result.nonexempt_lines)
                    self.assertEqual(fails, bool(result.errors))
                    self.assertEqual(2, result.raw_pairs)
                    self.assertEqual(1, result.exempt_pairs)

    def test_strict_records_reject_unknown_and_invalid_certification_shapes(self) -> None:
        for raw in (
            b'{"schema":"pinboard-duplication-exceptions/v1","detector_version":"5.1.2","records":[],"extra":true}',
            b'{"schema":"pinboard-duplication-exceptions/v1","detector_version":"changed","records":[]}',
        ):
            with self.assertRaises(msgspec.ValidationError):
                msgspec.json.decode(raw, type=policy.Exceptions)
        for field, value in (("reviewer_task_id", "not-a-task-uuid"), ("evidence_sha256", "not-a-digest")):
            raw_certificate = {
                "reviewer_task_id": "00000000-0000-4000-8000-000000000001",
                "reviewed_pair_sha256": "a" * 64,
                "evidence_sha256": "b" * 64,
            }
            raw_certificate[field] = value
            with self.subTest(field=field), self.assertRaises(msgspec.ValidationError):
                msgspec.json.decode(msgspec.json.encode(raw_certificate), type=policy.Certification)

    def test_real_pinned_native_baseline_third_copy_and_raw_statistics(self) -> None:
        detector_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "src"
            source_root.mkdir()
            body = (
                "def calculate(value):\n"
                + "".join(f"    value = value * {n} + {n + 1}\n" for n in range(1, 24))
                + "    return value\n"
            )
            (source_root / "first.py").write_text(body)
            (source_root / "second.py").write_text(body)
            (source_root / "unique.py").write_text("".join(f"independent_value_{n} = {n}\n" for n in range(10000)))
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "baseline",
                ],
                check=True,
            )
            initial = policy.run_detector(detector_root, root, source_root, "HEAD", root / "baseline-report")
            self.assertTrue(initial.duplicates)
            exceptions = policy.Exceptions(
                "pinboard-duplication-exceptions/v1",
                "5.1.2",
                tuple(certified_pair(p, source_root) for p in initial.duplicates),
            )
            clean = policy.evaluate(initial, exceptions, source_root)
            self.assertFalse(clean.errors)
            self.assertEqual(
                initial.statistics.total.duplicatedLines,
                sum(p.firstFile.end - p.firstFile.start for p in initial.duplicates),
            )
            self.assertAlmostEqual(
                initial.statistics.total.percentage,
                100 * initial.statistics.total.duplicatedLines / initial.statistics.total.lines,
            )
            (source_root / "middle.py").write_text(body)
            third = policy.run_detector(detector_root, root, source_root, "HEAD", root / "third-report")
            self.assertGreater(len(third.duplicates), len(initial.duplicates))
            self.assertTrue(any(p.isNew for p in third.duplicates))
            self.assertEqual("middle.py", third.duplicates[0].secondFile.name)
            self.assertFalse(third.duplicates[0].isNew)
            self.assertEqual("second.py", third.duplicates[1].secondFile.name)
            self.assertTrue(third.duplicates[1].isNew)
            ordered_third = policy.evaluate(third, exceptions, source_root)
            self.assertLess(ordered_third.percentage, 0.3)
            self.assertTrue(any("Unreviewed pair" in error for error in ordered_third.errors))
            self.assertEqual(
                msgspec.json.decode((root / "third-report/jscpd-report.json").read_bytes(), type=policy.NativeReport),
                third,
            )
            subprocess.run(["git", "-C", str(root), "add", "src/middle.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "native baseline includes third copy",
                ],
                check=True,
            )
            baseline_third = policy.run_detector(
                detector_root, root, source_root, "HEAD", root / "baseline-third-report"
            )
            self.assertFalse(any(p.isNew for p in baseline_third.duplicates))
            unreviewed_third = policy.evaluate(baseline_third, exceptions, source_root)
            self.assertLess(unreviewed_third.percentage, 0.3)
            self.assertTrue(any("Unreviewed pair" in error for error in unreviewed_third.errors))
            (source_root / "first.py").write_text(body.replace("value * 2 + 3", "value * 200 + 300"))
            changed = policy.run_detector(detector_root, root, source_root, "HEAD", root / "changed-report")
            self.assertTrue(policy.evaluate(changed, exceptions, source_root).errors)


if __name__ == "__main__":
    unittest.main()
