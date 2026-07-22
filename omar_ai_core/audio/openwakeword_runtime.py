from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path
from types import ModuleType


def _runtime_package() -> ModuleType:
    """Load openWakeWord without its optional training-only dependencies.

    openwakeword 0.6 imports scipy and scikit-learn from its package
    ``__init__`` even when only ONNX inference is requested. The desktop build
    deliberately excludes those large training libraries. This creates the
    package metadata needed by ``model.py`` and ``vad.py`` without importing
    the unused custom-verifier trainer.
    """
    existing = sys.modules.get("openwakeword")
    if existing is not None and getattr(existing, "_jarvis_inference_runtime", False):
        return existing

    spec = importlib.machinery.PathFinder.find_spec("openwakeword", sys.path)
    locations = list(spec.submodule_search_locations or []) if spec else []
    if not locations:
        raise ModuleNotFoundError("openwakeword package is not installed")
    package_dir = Path(locations[0])
    model_dir = package_dir / "resources" / "models"

    package = ModuleType("openwakeword")
    package.__file__ = str(package_dir / "__init__.py")
    package.__path__ = [str(package_dir)]
    package.__package__ = "openwakeword"
    package.__spec__ = importlib.machinery.ModuleSpec(
        "openwakeword",
        loader=None,
        is_package=True,
    )
    package.__spec__.submodule_search_locations = package.__path__
    package._jarvis_inference_runtime = True
    package.FEATURE_MODELS = {
        "embedding": {"model_path": str(model_dir / "embedding_model.onnx")},
        "melspectrogram": {"model_path": str(model_dir / "melspectrogram.onnx")},
    }
    package.VAD_MODELS = {
        "silero_vad": {"model_path": str(model_dir / "silero_vad.onnx")},
    }
    package.MODELS = {
        "hey_jarvis": {"model_path": str(model_dir / "hey_jarvis_v0.1.onnx")},
    }
    package.model_class_mappings = {}

    def get_pretrained_model_paths(inference_framework: str = "tflite") -> list[str]:
        suffix = ".onnx" if inference_framework == "onnx" else ".tflite"
        return [str(model_dir / f"hey_jarvis_v0.1{suffix}")]

    package.get_pretrained_model_paths = get_pretrained_model_paths
    sys.modules["openwakeword"] = package
    return package


def load_model_class():
    package = _runtime_package()
    from openwakeword.vad import VAD

    package.VAD = VAD
    from openwakeword.model import Model

    package.Model = Model
    return Model
