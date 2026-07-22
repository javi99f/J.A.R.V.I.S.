import time
import unittest
import sys
from unittest.mock import patch

from omar_ai_core.audio.wakeword import WakeWordGate


class WakeWordGateTests(unittest.TestCase):
    def test_explicit_followup_window_expires_without_extension(self):
        with patch("omar_ai_core.audio.wakeword.time.monotonic", return_value=100.0):
            gate = WakeWordGate(mode="manual")
            gate.activate_for(5)
        with patch("omar_ai_core.audio.wakeword.time.monotonic", return_value=103.0):
            gate.extend_conversation()
        with patch("omar_ai_core.audio.wakeword.time.monotonic", return_value=105.1):
            self.assertFalse(gate.active)

    def test_normal_threshold_peak_activates_immediately(self):
        class FakeModel:
            def predict(self, _samples):
                return {"hey jarvis": [0.41]}

        frame = b"\0" * (WakeWordGate.FRAME_SAMPLES * 2)
        gate = WakeWordGate(mode="manual", threshold=0.40, confirmation_frames=2)
        gate.mode = "wakeword"
        gate._model = FakeModel()
        self.assertTrue(gate.process(frame)[0])

    def test_confirmation_accepts_two_soft_peaks_in_one_phrase(self):
        class FakeModel:
            def __init__(self):
                self.scores = iter([0.26, 0.10, 0.27])

            def predict(self, _samples):
                return {"hey jarvis": [next(self.scores)]}

        frame = b"\0" * (WakeWordGate.FRAME_SAMPLES * 2)
        gate = WakeWordGate(mode="manual", threshold=0.40, confirmation_frames=2)
        gate.mode = "wakeword"
        gate._model = FakeModel()
        self.assertFalse(gate.process(frame)[0])
        self.assertFalse(gate.process(frame)[0])
        self.assertTrue(gate.process(frame)[0])

    def test_health_snapshot_exposes_safe_detector_telemetry(self):
        gate = WakeWordGate(mode="manual", threshold=0.40)
        snapshot = gate.health_snapshot()
        self.assertEqual(snapshot["threshold"], 0.4)
        self.assertIn("last_score", snapshot)
        self.assertIn("last_rms", snapshot)

    def test_inference_backend_does_not_load_training_dependencies(self):
        gate = WakeWordGate(mode="wakeword")
        self.assertTrue(gate.available, gate.error)
        self.assertIsNotNone(gate._model)
        self.assertNotIn("openwakeword.custom_verifier_model", sys.modules)

    def test_continuous_mode_is_always_active(self):
        gate = WakeWordGate(mode="continuous")
        self.assertTrue(gate.available)
        self.assertTrue(gate.active)
        gate.deactivate()
        self.assertTrue(gate.active)

    def test_manual_mode_is_privacy_safe(self):
        gate = WakeWordGate(mode="manual", conversation_seconds=3)
        self.assertFalse(gate.active)
        detected, score = gate.process(b"\0" * 2048)
        self.assertFalse(detected)
        self.assertEqual(score, 0.0)
        gate.activate()
        self.assertTrue(gate.active)
        gate.deactivate()
        self.assertFalse(gate.active)

    def test_rms_voice_threshold(self):
        gate = WakeWordGate(mode="manual", voice_rms_threshold=300)
        self.assertFalse(gate.contains_voice(b"\0" * 2048))
        loud = (1000).to_bytes(2, "little", signed=True) * 1024
        self.assertTrue(gate.contains_voice(loud))


if __name__ == "__main__":
    unittest.main()
