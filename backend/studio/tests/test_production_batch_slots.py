from types import SimpleNamespace

from django.test import SimpleTestCase

from studio.services.production_batch_slots import effective_batch_snapshot_for_task


class ProductionBatchSnapshotTests(SimpleTestCase):
    def test_reads_only_the_task_owned_batch_snapshot(self):
        snapshot = {
            "batch_id": "batch-1",
            "batch_index": 2,
            "batch_size": 3,
        }
        task = SimpleNamespace(capability_snapshot=snapshot)

        self.assertEqual(effective_batch_snapshot_for_task(task), snapshot)

    def test_missing_task_snapshot_is_not_inferred_from_related_rows(self):
        class TaskWithoutBatch:
            capability_snapshot = {}

            @property
            def deferred_generation_request(self):
                raise AssertionError("read-side batch inference must not run")

        self.assertEqual(effective_batch_snapshot_for_task(TaskWithoutBatch()), {})
