"""Partial startup must release every already-created isolated resource."""
from unittest import TestCase
from unittest.mock import Mock

from studio.tests.m8_page_bridge import isolated_case


class M8BridgeCleanupTests(TestCase):
    def test_database_setup_failure_cleans_media(self):
        runner = Mock()
        runner.setup_databases.side_effect = RuntimeError("database setup failed")
        with self.assertRaisesRegex(RuntimeError, "database setup failed"):
            with isolated_case(runner, Mock()):
                self.fail("must not yield")
        runner.teardown_test_environment.assert_called_once()
        runner.teardown_databases.assert_not_called()

    def test_partial_class_setup_cleans_registered_guards_and_database(self):
        runner, guard = Mock(), Mock()

        class Fixture(TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(guard)
                raise RuntimeError("class setup failed")

        with self.assertRaisesRegex(RuntimeError, "class setup failed"):
            with isolated_case(runner, Fixture):
                self.fail("must not yield")
        guard.assert_called_once()
        runner.teardown_databases.assert_called_once_with(runner.setup_databases.return_value)
        runner.teardown_test_environment.assert_called_once()

    def test_instance_setup_failure_cleans_all_ready_resources(self):
        runner, events = Mock(), []

        class Fixture(TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(lambda: events.append("class guard"))

            @classmethod
            def tearDownClass(cls):
                events.append("class transaction")

            def _pre_setup(self):
                self.addCleanup(lambda: events.append("instance guard"))

            def _post_teardown(self):
                events.append("test transaction")

            def setUp(self):
                raise RuntimeError("instance setup failed")

        with self.assertRaisesRegex(RuntimeError, "instance setup failed"):
            with isolated_case(runner, Fixture):
                self.fail("must not yield")
        self.assertEqual(events, ["instance guard", "test transaction", "class transaction", "class guard"])
        runner.teardown_databases.assert_called_once()
        runner.teardown_test_environment.assert_called_once()
