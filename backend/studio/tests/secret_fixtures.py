from __future__ import annotations

import base64
import os
from unittest.mock import patch

from studio.services.secret_store import create_secret_binding


TEST_KEY_ID = "test-suite-v1"
TEST_KEY_VALUE = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
TEST_SECRET_ENV = {
    "AUTOPROMPT_CONFIG_MASTER_KEYS": f"{TEST_KEY_ID}:{TEST_KEY_VALUE}",
    "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID": TEST_KEY_ID,
}


def enable_test_keyring(testcase) -> None:
    environment = patch.dict(os.environ, TEST_SECRET_ENV, clear=False)
    environment.start()
    testcase.addCleanup(environment.stop)


def create_test_secret(*, key: str, actor=None, value: str = "test-secret"):
    return create_secret_binding(
        key=key,
        label=key,
        purpose="provider_api",
        secret_value=value,
        actor=actor,
        reason="test_fixture",
    )
