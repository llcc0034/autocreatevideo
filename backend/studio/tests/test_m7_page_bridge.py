"""Pure tests for the opt-in bridge: failed isolation checks cannot exit green."""
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace

from studio.tests.m7_page_bridge import (
    class_cleanups, finish_fixture, instance_cleanups, retain_active_guards,
)


class BridgeCleanupTests(TestCase):
    def test_instance_cleanup_failure_does_not_skip_later_teardown(self):
        case = TestCase()
        case.addCleanup(Mock(side_effect=AssertionError("guard called")))
        later = Mock()
        with self.assertRaisesRegex(AssertionError, "instance.*assertion failed"):
            finish_fixture((("instance", lambda: instance_cleanups(case)), ("later", later)))
        later.assert_called_once()

    def test_class_cleanup_failure_does_not_skip_database_and_media_teardown(self):
        class Fixture(TestCase):
            pass
        Fixture.addClassCleanup(Mock(side_effect=AssertionError("network attempted")))
        later = Mock()
        with self.assertRaisesRegex(AssertionError, "network attempted"):
            finish_fixture((("class", lambda: class_cleanups(Fixture)), ("later", later)))
        later.assert_called_once()

    def test_effective_nested_guard_is_checked_even_if_caller_catches_denial(self):
        owner = SimpleNamespace(resolve=lambda: None)
        case = TestCase()
        with patch.object(owner, "resolve", side_effect=AssertionError("outer")) as outer:
            with patch.object(owner, "resolve", side_effect=AssertionError("inner")):
                check = retain_active_guards(case, [owner.resolve])
                with self.assertRaisesRegex(AssertionError, "inner"):
                    owner.resolve()
                outer.assert_not_called()
                with self.assertRaises(AssertionError):
                    check()
                with self.assertRaises(AssertionError):
                    instance_cleanups(case)

    def test_unused_guards_and_successful_teardown_pass(self):
        case = TestCase()
        guard = Mock()
        retain_active_guards(case, [guard])()
        later = Mock()
        finish_fixture((("instance", lambda: instance_cleanups(case)), ("later", later)))
        later.assert_called_once()
