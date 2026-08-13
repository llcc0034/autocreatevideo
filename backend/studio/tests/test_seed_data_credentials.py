from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase


class SeedDataCredentialTests(TestCase):
    def test_default_seed_does_not_create_demo_user(self):
        call_command("seed_data", stdout=StringIO(), verbosity=0)

        self.assertFalse(get_user_model().objects.filter(username="demo").exists())

    @patch("studio.management.commands.seed_data.secrets.token_urlsafe")
    def test_demo_uses_one_time_random_password_and_does_not_persist_plaintext(self, token_urlsafe):
        generated_password = "random-demo-password_7wG2PDKm3xVY4Nq9sL6cR8tB"
        token_urlsafe.return_value = generated_password
        output = StringIO()

        call_command("seed_data", "--with-demo-project", stdout=output, verbosity=0)

        token_urlsafe.assert_called_once_with(32)
        demo = get_user_model().objects.get(username="demo")
        self.assertTrue(demo.check_password(generated_password))
        self.assertNotIn(generated_password, demo.password)
        self.assertEqual(output.getvalue().count(generated_password), 1)

    @patch("studio.management.commands.seed_data.secrets.token_urlsafe")
    def test_repeated_demo_seed_does_not_reset_or_redisplay_password(self, token_urlsafe):
        generated_password = "first-random-demo-password_G8m2Wp5qR9t4Kx7v"
        token_urlsafe.return_value = generated_password
        first_output = StringIO()
        second_output = StringIO()

        call_command("seed_data", "--with-demo-project", stdout=first_output, verbosity=0)
        demo = get_user_model().objects.get(username="demo")
        original_hash = demo.password

        call_command("seed_data", "--with-demo-project", stdout=second_output, verbosity=0)
        demo.refresh_from_db()

        token_urlsafe.assert_called_once_with(32)
        self.assertEqual(demo.password, original_hash)
        self.assertTrue(demo.check_password(generated_password))
        self.assertIn(generated_password, first_output.getvalue())
        self.assertNotIn("One-time password", second_output.getvalue())
        self.assertNotIn(generated_password, second_output.getvalue())
