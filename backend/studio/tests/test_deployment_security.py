import os
import subprocess
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from autoprompt_backend.deployment_security import validate_production_configuration


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _valid_configuration():
    return {
        "profile": "production",
        "debug": False,
        "secret_key": "prod-G7v9!pQ2#xL4$kN8@mR6&zT3+wY5_cD1-fH0=sJ9%uB7*aE2",
        "database_engine": "django.db.backends.postgresql",
        "database_password": "database-P8z2_mQ7-vN4-xK9",
        "allowed_hosts": ["studio.example.net", "backend"],
        "cors_allowed_origins": ["https://studio.example.net"],
        "csrf_trusted_origins": ["https://studio.example.net"],
        "secure_ssl_redirect": True,
        "session_cookie_secure": True,
        "csrf_cookie_secure": True,
        "secure_hsts_seconds": 31536000,
    }


class ProductionConfigurationValidationTests(SimpleTestCase):
    def test_valid_production_configuration_passes(self):
        validate_production_configuration(**_valid_configuration())

    def test_development_profile_does_not_break_loopback_workflow(self):
        values = _valid_configuration()
        values.update({
            "profile": "development",
            "debug": True,
            "secret_key": "dev-only",
            "database_engine": "django.db.backends.sqlite3",
            "database_password": "",
            "allowed_hosts": ["127.0.0.1"],
            "cors_allowed_origins": ["http://127.0.0.1:5173"],
            "csrf_trusted_origins": [],
            "secure_ssl_redirect": False,
            "session_cookie_secure": False,
            "csrf_cookie_secure": False,
            "secure_hsts_seconds": 0,
        })
        validate_production_configuration(**values)

    def test_unsafe_production_values_fail_closed_with_all_reasons(self):
        values = _valid_configuration()
        values.update({
            "debug": True,
            "secret_key": "replace-with-secret",
            "database_engine": "django.db.backends.sqlite3",
            "database_password": "short",
            "allowed_hosts": ["*", "localhost"],
            "cors_allowed_origins": ["http://localhost:8080/path"],
            "csrf_trusted_origins": [],
            "secure_ssl_redirect": False,
            "session_cookie_secure": False,
            "csrf_cookie_secure": False,
            "secure_hsts_seconds": 0,
        })
        with self.assertRaises(ImproperlyConfigured) as caught:
            validate_production_configuration(**values)
        message = str(caught.exception)
        for expected in (
            "DJANGO_DEBUG",
            "DJANGO_SECRET_KEY",
            "PostgreSQL",
            "PostgreSQL password",
            "DJANGO_ALLOWED_HOSTS",
            "CORS_ALLOWED_ORIGINS",
            "CSRF_TRUSTED_ORIGINS",
            "SECURE_SSL_REDIRECT",
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "SECURE_HSTS_SECONDS",
        ):
            self.assertIn(expected, message)

    def test_origin_host_must_be_explicitly_allowed(self):
        values = _valid_configuration()
        values["cors_allowed_origins"] = ["https://other.example.net"]
        with self.assertRaisesMessage(ImproperlyConfigured, "absent from DJANGO_ALLOWED_HOSTS"):
            validate_production_configuration(**values)

    def test_real_settings_module_enforces_production_profile(self):
        environment = os.environ.copy()
        environment.update({
            "AUTOPROMPT_DEPLOYMENT_PROFILE": "production",
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": _valid_configuration()["secret_key"],
            "DATABASE_URL": "postgresql://autoprompt:database-P8z2_mQ7-vN4-xK9@db:5432/autoprompt",
            "DJANGO_ALLOWED_HOSTS": "studio.example.net,backend",
            "CORS_ALLOWED_ORIGINS": "https://studio.example.net",
            "CSRF_TRUSTED_ORIGINS": "https://studio.example.net",
            "SECURE_SSL_REDIRECT": "true",
            "SESSION_COOKIE_SECURE": "true",
            "CSRF_COOKIE_SECURE": "true",
            "SECURE_HSTS_SECONDS": "31536000",
        })
        command = [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.DEPLOYMENT_PROFILE)",
        ]
        valid = subprocess.run(
            command, cwd=BACKEND_ROOT, env=environment,
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout.strip(), "production")

        environment["DJANGO_DEBUG"] = "true"
        invalid = subprocess.run(
            command, cwd=BACKEND_ROOT, env=environment,
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("DJANGO_DEBUG must be false", invalid.stderr)
