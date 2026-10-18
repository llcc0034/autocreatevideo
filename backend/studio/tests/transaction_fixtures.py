"""Restore migration-owned test seeds after a preceding transaction test flush."""
from django.core.management import call_command
from django.test import TransactionTestCase


class SeededTransactionTestCase(TransactionTestCase):
    serialized_rollback = True

    @classmethod
    def _fixture_setup(cls):
        # A normal transaction test recreates permissions via post_migrate on
        # flush, but not the configuration data migrations. Empty those rows
        # before restoring Django's original, isolated leaf-schema snapshot.
        for db_name in cls._databases_names(include_mirrors=False):
            call_command("flush", verbosity=0, interactive=False, database=db_name,
                reset_sequences=False, inhibit_post_migrate=True)
        super()._fixture_setup()
