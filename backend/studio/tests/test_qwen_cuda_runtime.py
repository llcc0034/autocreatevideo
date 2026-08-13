from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.services.prompt_translation import qwen_runtime_device_state


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SCRIPT = PROJECT_ROOT / "scripts" / "qwen_translate_once.py"


def _load_qwen_runtime_module():
    name = "qwen_translate_once_cuda_test"
    spec = importlib.util.spec_from_file_location(name, RUNTIME_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class QwenCudaRuntimeScriptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runtime = _load_qwen_runtime_module()

    def test_cpu_keeps_zero_gpu_layers_even_when_global_value_is_all_layers(self):
        calls = []

        class FakeLlama:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        fake_llama_cpp = SimpleNamespace(
            Llama=FakeLlama,
            __version__="test",
            llama_supports_gpu_offload=lambda: True,
        )
        with patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp}):
            _, runtime = self.runtime._create_llama_model(
                model_path=Path("model.gguf"), context_size=512, threads=1, seed=43,
                payload={"device": "cpu", "gpu_layers": -1},
            )

        self.assertEqual(runtime["device"], "cpu")
        self.assertFalse(runtime["gpu_offload_supported"])
        self.assertEqual(calls[0]["n_gpu_layers"], 0)

    def test_cuda_requires_gpu_offload_and_passes_explicit_layer_count(self):
        calls = []

        class FakeLlama:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        fake_llama_cpp = SimpleNamespace(
            Llama=FakeLlama,
            __version__="test",
            llama_supports_gpu_offload=lambda: True,
        )
        with patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp}):
            _, runtime = self.runtime._create_llama_model(
                model_path=Path("model.gguf"), context_size=512, threads=1, seed=43,
                payload={"device": "cuda", "gpu_layers": -1},
            )

        self.assertEqual(runtime["device"], "cuda")
        self.assertTrue(runtime["gpu_offload_supported"])
        self.assertEqual(calls[0]["n_gpu_layers"], -1)

    def test_cuda_rejects_zero_layers_and_runtime_without_offload(self):
        fake_llama_cpp = SimpleNamespace(
            Llama=object,
            __version__="test",
            llama_supports_gpu_offload=lambda: False,
        )
        with patch.dict(sys.modules, {"llama_cpp": fake_llama_cpp}):
            with self.assertRaises(self.runtime.RuntimeInputError):
                self.runtime._llama_runtime({"device": "cuda", "gpu_layers": -1})
        with self.assertRaises(self.runtime.RuntimeInputError):
            self.runtime._runtime_gpu_layers({"gpu_layers": 0}, device="cuda")


class QwenCudaCapabilityTests(SimpleTestCase):
    def _config(self, root: Path) -> dict:
        runtime_python = root / "python.exe"
        runtime_script = root / "qwen_translate_once.py"
        runtime_python.write_text("", encoding="utf-8")
        runtime_script.write_text("", encoding="utf-8")
        model_dir = root / "model"
        model_dir.mkdir()
        return {
            "device": "cuda",
            "gpu_layers": -1,
            "runtime_python": runtime_python,
            "runtime_script": runtime_script,
            "model_dir": model_dir,
            "model_id": "Qwen/Qwen3-4B-GGUF",
        }

    def test_cuda_capability_requires_a_successful_context_smoke(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp))
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "ok": True,
                    "metadata": {
                        "device": "cuda",
                        "runtime_version": "0.3.34",
                        "gpu_offload_supported": True,
                        "gpu_context_smoke_passed": True,
                        "n_gpu_layers": -1,
                    },
                }),
            )
            with patch(
                "studio.services.prompt_translation.local_qwen_runtime_lock",
                return_value=nullcontext(),
            ), patch(
                "studio.services.prompt_translation.subprocess.run", return_value=completed,
            ) as run:
                state = qwen_runtime_device_state(config)

        self.assertTrue(state["gpu_ready"])
        self.assertTrue(state["runtime_ready"])
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent["operation"], "runtime_probe")
        self.assertTrue(sent["smoke_test"])
        self.assertEqual(sent["gpu_layers"], -1)

    def test_cuda_capability_fails_closed_when_context_smoke_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self._config(Path(temp))
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "ok": True,
                    "metadata": {
                        "device": "cuda",
                        "gpu_offload_supported": True,
                        "gpu_context_smoke_passed": False,
                        "n_gpu_layers": -1,
                    },
                }),
            )
            with patch(
                "studio.services.prompt_translation.local_qwen_runtime_lock",
                return_value=nullcontext(),
            ), patch(
                "studio.services.prompt_translation.subprocess.run", return_value=completed,
            ):
                state = qwen_runtime_device_state(config)

        self.assertFalse(state["gpu_ready"])
        self.assertFalse(state["runtime_ready"])
        self.assertEqual(state["runtime_probe_error_code"], "runtime_probe_failed")

    def test_cpu_capability_never_starts_a_cuda_probe(self):
        with patch("studio.services.prompt_translation.subprocess.run") as run:
            state = qwen_runtime_device_state({"device": "cpu", "gpu_layers": -1})

        self.assertTrue(state["runtime_ready"])
        self.assertFalse(state["cuda_requested"])
        self.assertFalse(state["gpu_ready"])
        run.assert_not_called()
