from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

import validate_production_deployment as deployment


SOURCE_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    "backend/.env.example",
    "frontend/.env.example",
    "docker/backend.Dockerfile",
    "docker/frontend.Dockerfile",
    "docker/backend-entrypoint.sh",
    "docker/nginx.conf",
    "docker/nginx-main.conf",
    "docker/security-headers.conf",
    "scripts/restart.ps1",
)


class ProductionDeploymentValidatorTamperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in REQUIRED_FILES:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE_ROOT / relative, destination)
        self.original_root = deployment.ROOT
        deployment.ROOT = self.root

    def tearDown(self):
        deployment.ROOT = self.original_root
        self.temporary.cleanup()

    def _replace(self, relative: str, old: str, new: str):
        path = self.root / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_repository_profile_passes(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            deployment.main()
        self.assertIn("static validation passed", output.getvalue())

    def test_public_frontend_port_is_rejected(self):
        self._replace(
            "docker-compose.yml",
            '"127.0.0.1:${FRONTEND_PORT:-8080}:8080"',
            '"${FRONTEND_PORT:-8080}:8080"',
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "loopback-published"):
            deployment.main()

    def test_broad_library_alias_is_rejected(self):
        self._replace(
            "docker/nginx.conf",
            "alias /srv/autoprompt-assets/library/;",
            "alias /srv/autoprompt-assets/;",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "shadow Vite assets"):
            deployment.main()

    def test_public_library_bypass_is_rejected(self):
        self._replace("docker/nginx.conf", "        internal;\n", "")
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "authorization cannot be bypassed"):
            deployment.main()

    def test_removing_non_root_backend_user_is_rejected(self):
        self._replace("docker/backend.Dockerfile", "USER 10001:10001", "USER root")
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "non-root UID"):
            deployment.main()

    def test_missing_browser_security_header_is_rejected(self):
        self._replace(
            "docker/security-headers.conf",
            'add_header X-Frame-Options "DENY" always;\n',
            "",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "X-Frame-Options"):
            deployment.main()

    def test_implicit_backend_base_image_tag_is_rejected(self):
        self._replace("docker/backend.Dockerfile", "FROM python:3.11-slim", "FROM python")
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "implicit/latest"):
            deployment.main()

    def test_internal_healthcheck_without_https_signal_is_rejected(self):
        self._replace(
            "docker-compose.yml",
            ",'X-Forwarded-Proto':'https'",
            "",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "production redirect"):
            deployment.main()

    def test_local_restart_lan_binding_is_rejected(self):
        self._replace(
            "scripts/restart.ps1",
            "runserver', '127.0.0.1:8000'",
            "runserver', '0.0.0.0:8000'",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "loopback-only"):
            deployment.main()

    def test_missing_database_encryption_keyring_passthrough_is_rejected(self):
        self._replace(
            "docker-compose.yml",
            '  AUTOPROMPT_CONFIG_MASTER_KEYS: "${AUTOPROMPT_CONFIG_MASTER_KEYS:?set the database credential encryption keyring in the deployment secret manager}"\n',
            "",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "AUTOPROMPT_CONFIG_MASTER_KEYS"):
            deployment.main()

    def test_long_running_entrypoint_must_not_log_temporary_password(self):
        self._replace(
            "docker/backend-entrypoint.sh",
            "  python manage.py migrate --noinput\n",
            "  python manage.py migrate --noinput\n  python manage.py bootstrap_superadmin --if-missing\n",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "temporary superadministrator password"):
            deployment.main()

    def test_frontend_mock_environment_key_is_rejected(self):
        self._replace(
            "frontend/.env.example",
            "VITE_API_BASE_URL=/api/v1\n",
            "VITE_API_BASE_URL=/api/v1\nVITE_MOCK_PRODUCTION_MAX_CONCURRENT=1\n",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "mock controls"):
            deployment.main()

    def test_frontend_unapproved_environment_key_is_rejected(self):
        self._replace(
            "frontend/.env.example",
            "VITE_API_BASE_URL=/api/v1\n",
            "VITE_API_BASE_URL=/api/v1\nVITE_FEATURE_FLAG=true\n",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "only VITE_API_BASE_URL"):
            deployment.main()

    def test_retired_qwen_environment_key_is_rejected(self):
        self._replace(
            "backend/.env.example",
            "TASK_MAX_CONCURRENT=1\n",
            "TASK_MAX_CONCURRENT=1\nQWEN_GPU_LAYERS=0\n",
        )
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "retired local-process setting QWEN_GPU_LAYERS"):
            deployment.main()

    def test_backup_archives_entering_build_context_are_rejected(self):
        self._replace(".dockerignore", "backups\nbackups/**\n", "")
        with self.assertRaisesRegex(deployment.DeploymentValidationError, "backup archives"):
            deployment.main()


if __name__ == "__main__":
    unittest.main()
