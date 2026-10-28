"""Abbreviation compatibility preserves every other byte and the raw evidence identity."""

import unittest
from itertools import product

from pinboard.application.candidate_identity import candidate_diffs_match, working_tree_identity


class CandidateDiffIdentityTest(unittest.TestCase):
    def test_only_index_header_prefix_expansion_is_equivalent(self) -> None:
        original_id = b"1234567890" * 4
        changed_id = b"abcdef0123" * 4

        def patch(width: int) -> bytes:
            return (
                b"diff --git a/tracked.txt b/tracked.txt\nindex "
                + original_id[:width]
                + b".."
                + changed_id[:width]
                + b" 100644\n--- a/tracked.txt\n+++ b/tracked.txt\n"
                b"@@ -1,2 +1,2 @@\n index 1234567..abcdef0\n-base\n+candidate\n"
            )

        patches = tuple(patch(width) for width in (7, 8, 40))
        self.assertEqual(3, len({working_tree_identity("a" * 40, value) for value in patches}))
        for accepted, observed in product(patches, repeat=2):
            with self.subTest(accepted=accepted, observed=observed):
                self.assertTrue(candidate_diffs_match(accepted, observed))
        full = patches[-1]
        for label, drifted in (
            ("blob", full.replace(original_id, b"f" * 40)),
            ("path", full.replace(b"a/tracked.txt", b"a/other.txt")),
            ("mode", full.replace(b" 100644\n", b" 100755\n")),
            ("hunk", full.replace(b"@@ -1,2 +1,2 @@", b"@@ -2,2 +2,2 @@")),
            ("content", full.replace(b"+candidate", b"+different")),
            ("context-index-text", full.replace(b" index 1234567", b" index 12345678")),
            ("line-ending", full.replace(b"--- a/tracked.txt\n", b"--- a/tracked.txt\r\n")),
            ("missing-newline", full[:-1]),
            ("extra-line", full + b"\n"),
            ("header-outside-file", full.split(b"\n", 1)[1]),
        ):
            with self.subTest(drift=label):
                self.assertNotEqual(full, drifted)
                self.assertFalse(candidate_diffs_match(patches[0], drifted))
        self.assertFalse(candidate_diffs_match(b"index 1234567..abcdef0\n", b"index 12345678..abcdef01\n"))

    def test_binary_payload_and_mode_only_changes_remain_exact(self) -> None:
        binary = (
            b"diff --git a/binary b/binary\nindex "
            + b"a" * 40
            + b".."
            + b"b" * 40
            + b" 100644\nGIT binary patch\nliteral 4\nLc${Nk&j0jE0|;Ea\n\nliteral 4\nLc${Nk&j0jF0|;Eb\n\n"
        )
        abbreviated = binary.replace(b"a" * 40, b"a" * 7).replace(b"b" * 40, b"b" * 8)
        self.assertTrue(candidate_diffs_match(abbreviated, binary))
        self.assertFalse(candidate_diffs_match(abbreviated, binary.replace(b"&j0jE", b"&j0jG")))
        modes = b"diff --git a/file b/file\nold mode 100644\nnew mode 100755\n"
        self.assertTrue(candidate_diffs_match(modes, modes))
        self.assertFalse(candidate_diffs_match(modes, modes.replace(b"100755", b"100644")))
        self.assertFalse(candidate_diffs_match(b"", modes))
