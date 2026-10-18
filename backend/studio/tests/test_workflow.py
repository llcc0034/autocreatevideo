from django.test import SimpleTestCase

from studio.services.workflow import (
    aligned_frames,
)
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compiler_contract_from_input,
)


class WorkflowTests(SimpleTestCase):
    def test_retired_minimax_h3_v1_compiler_is_not_registered(self):
        with self.assertRaisesRegex(WorkflowCompilerError, "未注册"):
            compiler_contract_from_input({
                "compiler": {"id": "minimax_h3", "version": 1},
            })

    def test_removed_specialized_compilers_are_not_registered(self):
        for version in (2, 3):
            with self.assertRaisesRegex(WorkflowCompilerError, "未注册"):
                compiler_contract_from_input({"compiler": {"id": "minimax_h3", "version": version}})

    def test_h3_alignment_uses_nearest_legal_frame_count(self):
        policy = {
            "fps": 24,
            "minimum_frames": 5,
            "maximum_frames": 3600,
            "alignment_modulus": 17,
            "alignment_remainder": 5,
        }
        self.assertEqual(aligned_frames(0, policy), 5)
        self.assertEqual(aligned_frames(900, policy), 22)
        self.assertEqual(aligned_frames(1000, policy), 22)
        self.assertEqual(aligned_frames(1250, policy), 22)
        self.assertEqual(aligned_frames(1300, policy), 39)
        self.assertEqual(aligned_frames(5000, policy), 124)
