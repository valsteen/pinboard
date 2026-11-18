import textwrap
import unittest
from pathlib import Path

from scripts import check_raising_arms
from scripts.check_raising_arms import Exemption, raising_arms, raising_guards, violations


def _arms(body: str) -> tuple[check_raising_arms.RaisingArm, ...]:
    return raising_arms("module.py", textwrap.dedent(body))


def _guards(body: str) -> tuple[check_raising_arms.RaisingGuard, ...]:
    return raising_guards("module.py", textwrap.dedent(body))


class RaisingArmCheckTest(unittest.TestCase):
    def test_arms_whose_body_only_raises_are_reported_with_their_location(self) -> None:
        source = """
            from enum import Enum

            class Kind(Enum):
                A = 1
                B = 2
                C = 3

            def named(kind: Kind) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case Kind.B:
                        raise ValueError("b")

            def alternatives(kind: Kind) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case Kind.B | Kind.C:
                        raise RuntimeError("b or c")

            def wildcard(kind: Kind) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case _:
                        raise ValueError("other")

            def bound(kind: Kind) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case _ as other:
                        raise ValueError(other)

            def built(kind: Kind) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case Kind.B:
                        error = ValueError("b")
                        raise error

            def cleaned(kind: Kind, path: str) -> int:
                match kind:
                    case Kind.A:
                        return 1
                    case Kind.B:
                        with open(path) as handle:
                            handle.close()
                            raise ValueError("b")

            def guarded(code: int) -> str:
                match code:
                    case 0:
                        return "ok"
                    case value if value < 0:
                        raise ValueError(value)

            def text(value: str) -> int:
                match value:
                    case "a":
                        return 1
                    case _:
                        raise ValueError(value)

            class Owner:
                def method(self, kind: Kind) -> int:
                    match kind:
                        case Kind.A:
                            return 1
                        case _:
                            raise ValueError(kind)
        """
        self.assertEqual(
            [
                ("named", "Kind.B", 13),
                ("alternatives", "Kind.B | Kind.C", 20),
                ("wildcard", "_", 27),
                ("bound", "_ as other", 34),
                ("built", "Kind.B", 41),
                ("cleaned", "Kind.B", 49),
                ("guarded", "value if value < 0", 58),
                ("text", "_", 65),
                ("Owner.method", "_", 73),
            ],
            [(arm.function, arm.pattern, arm.line) for arm in _arms(source)],
        )

    def test_arms_that_can_complete_return_or_assert_never_are_not_reported(self) -> None:
        source = """
            from typing import assert_never

            def select(kind: int, flag: bool) -> int:
                match kind:
                    case 0:
                        if flag:
                            return 0
                        raise ValueError(kind)
                    case 1:
                        pass
                    case 2:
                        if flag:
                            raise ValueError(kind)
                    case 3:
                        def nested() -> int:
                            return 1
                        raise ValueError(nested())
                    case _ as unreachable:
                        assert_never(unreachable)
                for value in range(kind):
                    match value:
                        case 4:
                            continue
                        case 5:
                            break
                return 1
        """
        self.assertEqual([("select", "3")], [(arm.function, arm.pattern) for arm in _arms(source)])

    def test_exemptions_hide_their_arm_and_a_stale_exemption_fails(self) -> None:
        arms = _arms(
            """
            def decode(version: int) -> str:
                match version:
                    case 1:
                        return "v1"
                    case _:
                        raise ValueError(version)
            """
        )
        exempted = Exemption("module.py", "decode", "_", "Stored-schema decoding.")
        self.assertEqual((), violations(arms, (), (exempted,)))
        unexempted = violations(arms, (), ())
        self.assertEqual(1, len(unexempted))
        self.assertIn("src/pinboard/module.py:6: decode: match arm `case _` only raises", unexempted[0])
        stale = violations((), (), (exempted,))
        self.assertEqual(1, len(stale))
        self.assertIn("matches no raising arm or isinstance guard", stale[0])

    def test_isinstance_guards_that_only_raise_assertion_error_are_reported(self) -> None:
        source = """
            def negated(value: int | str) -> int:
                if not isinstance(value, int):
                    raise AssertionError("an int")
                return value

            def plain(value: int | str) -> str:
                if isinstance(value, int):
                    raise AssertionError
                return value

            def both(value: int | str, flag: bool) -> int | str:
                if isinstance(value, int) and flag:
                    if value:
                        raise AssertionError("nonzero")
                    else:
                        raise AssertionError("zero")
                return value

            class Owner:
                def method(self, value: int | str) -> int | str:
                    if isinstance(value, str):
                        message = "a string"
                        raise AssertionError(message)
                    return value
        """
        self.assertEqual(
            [
                ("negated", "not isinstance(value, int)", 3),
                ("plain", "isinstance(value, int)", 8),
                ("both", "isinstance(value, int) and flag", 13),
                ("Owner.method", "isinstance(value, str)", 22),
            ],
            [(guard.function, guard.test, guard.line) for guard in _guards(source)],
        )

    def test_guards_raising_other_exceptions_or_able_to_complete_are_not_reported(self) -> None:
        source = """
            def other(value: int | str) -> int:
                if not isinstance(value, int):
                    raise ValueError(value)
                if isinstance(value, bool):
                    raise TypeError(value)
                return value

            def partial(value: int | str, flag: bool) -> int | str:
                if isinstance(value, int):
                    if flag:
                        raise AssertionError("flagged")
                if isinstance(value, str):
                    if flag:
                        return value
                    raise AssertionError("unflagged")
                if value == 0:
                    raise AssertionError("zero")
                assert isinstance(value, int)
                return value
        """
        self.assertEqual((), _guards(source))
        self.assertEqual((), _arms(source))

    def test_guard_exemptions_hide_their_guard_and_a_stale_guard_exemption_fails(self) -> None:
        guards = _guards(
            """
            def publish(cause: Exception) -> None:
                if not isinstance(cause, OSError):
                    raise AssertionError(cause)
            """
        )
        exempted = Exemption("module.py", "publish", "not isinstance(cause, OSError)", "Filesystem effect boundary.")
        self.assertEqual((), violations((), guards, (exempted,)))
        unexempted = violations((), guards, ())
        self.assertEqual(1, len(unexempted))
        self.assertIn(
            "src/pinboard/module.py:3: publish: isinstance guard `if not isinstance(cause, OSError)` only raises "
            "AssertionError",
            unexempted[0],
        )
        stale = violations((), (), (exempted,))
        self.assertEqual(1, len(stale))
        self.assertIn("`not isinstance(cause, OSError)` matches no raising arm or isinstance guard", stale[0])

    def test_production_tree_has_no_unexempted_raising_arm_or_stale_exemption(self) -> None:
        self.assertEqual((), check_raising_arms.check_production_tree(Path(__file__).resolve().parent.parent))


if __name__ == "__main__":
    unittest.main()
