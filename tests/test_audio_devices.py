import asyncio
import unittest
from unittest.mock import patch

import numpy as np

from omar_ai_core import runtime
from omar_ai_core.display.liquid_window import (
    _canonical_audio_device,
    enumerate_audio_devices,
)


class _AudioSettingsUI:
    def __init__(self):
        self.logs = []
        self.refresh_count = 0

    def write_log(self, message):
        self.logs.append(message)

    def refresh_audio_devices(self):
        self.refresh_count += 1


class AudioDeviceEnumerationTests(unittest.TestCase):
    def test_devices_are_filtered_by_input_and_output_channels(self):
        devices = [
            {"name": "Mapper - Input", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Microphone (USB Audio)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Speakers (USB Audio)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Disabled", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 0},
        ]
        hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]

        self.assertEqual(
            enumerate_audio_devices("input", devices, hostapis),
            [("USB Audio", 1)],
        )
        self.assertEqual(
            enumerate_audio_devices("output", devices, hostapis),
            [("USB Audio", 2)],
        )

    def test_legacy_backend_selection_maps_to_the_same_wasapi_endpoint(self):
        devices = [
            {"name": "Speakers (USB Audio)", "hostapi": 0},
            {"name": "Speakers (USB Audio)", "hostapi": 1},
        ]
        self.assertEqual(
            _canonical_audio_device(0, devices, [("USB Audio", 1)]),
            1,
        )

    def test_reused_portaudio_index_is_mapped_by_stable_device_name(self):
        devices = [
            {"name": "Unrelated"},
            {"name": "New Device"},
            {"name": "Speakers (USB Audio)"},
        ]
        self.assertEqual(
            _canonical_audio_device(
                1,
                devices,
                [("New Device", 1), ("USB Audio", 2)],
                "USB Audio",
            ),
            2,
        )

    def test_missing_saved_endpoint_does_not_bind_to_reused_index(self):
        devices = [
            {"name": "Unrelated"},
            {"name": "New Device"},
        ]
        self.assertIsNone(
            _canonical_audio_device(
                1,
                devices,
                [("New Device", 1)],
                "USB Audio",
            )
        )

    def test_pcm_is_resampled_and_expanded_for_stereo_hardware(self):
        source = np.arange(240, dtype="<i2").tobytes()
        converted = runtime._convert_pcm16(source, 24000, 48000, 1, 2)
        samples = np.frombuffer(converted, dtype="<i2").reshape(-1, 2)
        self.assertEqual(samples.shape, (480, 2))
        np.testing.assert_array_equal(samples[:, 0], samples[:, 1])

    def test_endpoint_default_rate_is_preferred_for_compatibility(self):
        with (
            patch.object(
                runtime.sd,
                "query_devices",
                return_value={"max_output_channels": 2, "default_samplerate": 48000},
            ),
            patch.object(runtime.sd, "check_output_settings") as check,
        ):
            self.assertEqual(
                runtime._audio_stream_format(7, "output", 24000),
                (48000, 1),
            )
            check.assert_called_once_with(
                device=7, channels=1, dtype="int16", samplerate=48000
            )

    def test_runtime_startup_remaps_saved_endpoint_name(self):
        devices = [
            {"name": "Speakers (Other)", "hostapi": 0, "max_output_channels": 2},
            {"name": "Speakers (USB Audio)", "hostapi": 0, "max_output_channels": 2},
        ]
        secrets = {
            "OUTPUT_DEVICE": "0",
            "OUTPUT_DEVICE_NAME": "USB Audio",
        }
        with (
            patch.object(runtime, "get_secret", side_effect=lambda name, default="": secrets.get(name, default)),
            patch.object(runtime.sd, "query_devices", return_value=devices),
            patch.object(runtime.sd, "query_hostapis", return_value=[{"name": "Windows WASAPI"}]),
        ):
            self.assertEqual(runtime._configured_audio_device("OUTPUT_DEVICE"), 1)


class AudioDeviceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_change_restarts_only_the_selected_streams(self):
        live = object.__new__(runtime.JarvisLive)
        live.ui = _AudioSettingsUI()
        live._loop = asyncio.get_running_loop()
        live.audio_in_queue = asyncio.Queue()
        live.audio_in_queue.put_nowait(b"stale audio")
        live._input_device = None
        live._output_device = None
        live._input_device_generation = 0
        live._output_device_generation = 0

        live._on_audio_devices_changed(4, 9)
        await asyncio.sleep(0)

        self.assertEqual(live._input_device, 4)
        self.assertEqual(live._output_device, 9)
        self.assertEqual(live._input_device_generation, 1)
        self.assertEqual(live._output_device_generation, 1)
        self.assertIsNone(live.audio_in_queue.get_nowait())
        self.assertEqual(len(live.ui.logs), 2)

        live._on_audio_devices_changed(4, 9)
        await asyncio.sleep(0)
        self.assertEqual(live._input_device_generation, 1)
        self.assertEqual(live._output_device_generation, 1)

    async def test_backend_refresh_rebuilds_devices_without_restarting_jarvis(self):
        live = object.__new__(runtime.JarvisLive)
        live.ui = _AudioSettingsUI()
        live.audio_in_queue = asyncio.Queue()
        live._input_stream_open = False
        live._output_stream_open = False
        live._input_device_generation = 2
        live._output_device_generation = 3
        live._audio_backend_refreshing = False
        live._audio_backend_refresh_pending = True

        with patch.object(runtime, "_restart_portaudio_backend") as restart:
            await live._refresh_audio_backend()

        restart.assert_called_once()
        self.assertEqual(live._input_device_generation, 3)
        self.assertEqual(live._output_device_generation, 4)
        self.assertFalse(live._audio_backend_refresh_pending)
        self.assertFalse(live._audio_backend_refreshing)
        self.assertEqual(live.ui.refresh_count, 1)
        self.assertIsNone(live.audio_in_queue.get_nowait())
        self.assertTrue(any("actualizada" in line for line in live.ui.logs))


if __name__ == "__main__":
    unittest.main()
