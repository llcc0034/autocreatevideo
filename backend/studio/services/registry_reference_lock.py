from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from threading import RLock
from typing import Callable, Iterator, TypeVar

from django.db import connection, transaction


# One database-wide lock protects references that cannot be enforced by a
# database foreign key.  FK references remain protected by PostgreSQL itself;
# this lock closes the gap for recipe/config JSON and immutable Prompt Binding
# identities frozen into production runs, prompt artifacts, generation
# attempts and deployment-smoke request snapshots.
_REGISTRY_REFERENCE_LOCK_CLASS = 1_296_381_989
_REGISTRY_REFERENCE_LOCK_ID = 1
_LOCAL_REFERENCE_LOCK = RLock()
_Result = TypeVar("_Result")


@contextmanager
def registry_reference_write_guard() -> Iterator[None]:
    """Serialize formal non-FK reference writers with registry deletion.

    Production PostgreSQL uses a transaction-scoped advisory lock shared by
    every supported writer.  SQLite development/tests additionally use one
    in-process re-entrant lock because it has no advisory-lock primitive.
    """

    with _LOCAL_REFERENCE_LOCK:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        [_REGISTRY_REFERENCE_LOCK_CLASS, _REGISTRY_REFERENCE_LOCK_ID],
                    )
            yield


def registry_reference_writer(function: Callable[..., _Result]) -> Callable[..., _Result]:
    @wraps(function)
    def guarded(*args, **kwargs):
        with registry_reference_write_guard():
            return function(*args, **kwargs)

    return guarded
