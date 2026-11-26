"""Regression checks for detector display limits and final-run plot exports."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from lightkeeper.display import display_channels
from lightkeeper.runplots import export_run_figures
from lightkeeper.runview import broad_band_weights, broad_metrics


class TestDisplayAndReport(unittest.TestCase):
    def test_detector_padding_in_detector_order(self):
        self.assertEqual(display_channels("MayaPro2000", 2068), slice(10, 2058))
        self.assertEqual(display_channels("Maya2000 Pro", 2304), slice(10, 2058))
        self.assertEqual(display_channels("Unknown", 100, np.zeros(100)), slice(None))
        padded = np.r_[np.zeros(20), np.full(80, 100), np.zeros(30)]
        self.assertEqual(display_channels("Unknown", 130, padded), slice(20, 100))
        usb650 = np.r_[0, np.full(649, 255)]
        self.assertEqual(display_channels("USB650", 650, usb650), slice(1, None))

    def test_broad_metrics_keep_axis_direction_and_omit_invalid_slots(self):
        wavelengths = np.arange(800, 380, -0.5)
        expected = np.ones((2, len(wavelengths)))
        bg, fr = broad_metrics(expected, broad_band_weights(wavelengths), np.array([True, False]))
        self.assertAlmostEqual(bg[0], 1)
        self.assertAlmostEqual(fr[0], 1 / 7)
        self.assertTrue(np.isnan(bg[1]) and np.isnan(fr[1]))

    def test_export_preserves_counts_axis_and_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "spectra_example.h5"
            with h5py.File(source, "w") as h:
                h["wavelength_nm_device"] = [760, 720, 700]
                h["blocks/mean"] = [[2, 3, -2], [3, 4, 5], [20, 30, 40]]
                h["blocks/t_wall_ns"] = 1_700_000_000_000_000_000 + np.array([0, 1, 10]) * 10**9
                h["blocks/int_us"] = [20_000, 20_000, 10_000]
                h["dark_scans/session"] = [1, 1, 2]
                h["dark_scans/label"] = [b"pre", b"pre", b"post"]
                h["dark_scans/int_us"] = [20_000, 20_000, 20_000]
                h["dark_scans/spectrum"] = [[1, 0, 1], [1, 2, 1], [1, 1, 4]]
            before = source.read_bytes()
            with patch("lightkeeper.runplots._write_figure", return_value="test.png") as captured, patch(
                "lightkeeper.runplots._write_qc_figure", return_value="qc.png"
            ):
                export_run_figures(folder, {"plan": {"slot_s": 1}})
            raw, rate = [call.args for call in captured.call_args_list]
            np.testing.assert_array_equal(raw[2], [760, 720, 700])
            np.testing.assert_array_equal(raw[3][0][1][0], [2, 3, -2])
            self.assertLess(raw[3][0][0][-1], raw[3][1][0][0])
            self.assertAlmostEqual(rate[3][0][1][0, 0], np.log10(50), places=5)
            np.testing.assert_array_equal(rate[3][0][1][:, 1:], np.float32(rate[4][0]))
            self.assertTrue(np.isnan(rate[3][1][1]).all())
            first = export_run_figures(folder, {"plan": {"slot_s": 1}})
            self.assertEqual(first["log_slots_without_matched_dark"], 1)
            self.assertEqual(first["log_pixels_below_floor"], 4)
            self.assertEqual(first["qc_overview"], "run_qc_overview.png")
            second = export_run_figures(folder, {"plan": {"slot_s": 1}})
            self.assertNotEqual(first["raw_counts"], second["raw_counts"])
            self.assertNotEqual(first["qc_overview"], second["qc_overview"])
            for report in (first, second):
                for key in ("raw_counts", "log10_counts_per_s", "qc_overview"):
                    self.assertTrue((folder / report[key]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            with patch("lightkeeper.runplots._write_qc_figure", side_effect=ValueError("test QC failure")):
                third = export_run_figures(folder, {"plan": {"slot_s": 1}})
            self.assertEqual(third["qc_overview_error"], "ValueError: test QC failure")
            self.assertTrue((folder / third["raw_counts"]).is_file())
            self.assertTrue((folder / third["log10_counts_per_s"]).is_file())
            self.assertEqual(source.read_bytes(), before)

    def test_qc_uses_matched_darks_and_draws_acquisition_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wavelengths = np.arange(800, 380, -0.5)
            n = len(wavelengths)
            means = np.repeat(np.array([[15], [11], [15], [15], [15]]), n, axis=1)
            hot = int(np.flatnonzero(wavelengths == 660)[0])
            means[:, hot] = 5000
            with h5py.File(folder / "spectra_example.h5", "w") as h:
                h.attrs["hot_pixels"] = f"[{hot}]"
                h["wavelength_nm_device"] = wavelengths
                h["blocks/mean"] = means
                h["blocks/t_wall_ns"] = 1_700_000_000_000_000_000 + np.array([0, 1, 2, 3, 10]) * 10**9
                h["blocks/int_us"] = [20_000, 20_000, 20_000, 10_000, 20_000]
                h["blocks/n_saturated_scans"] = [0, 0, 3, 0, 0]
                h["dark_scans/session"] = [1, 1, 2]
                h["dark_scans/label"] = [b"pre", b"pre", b"post"]
                h["dark_scans/int_us"] = [20_000, 20_000, 20_000]
                h["dark_scans/spectrum"] = np.repeat([[10], [10], [11]], n, axis=1)
            with patch("lightkeeper.runplots._write_figure", return_value="spectrogram.png"), patch(
                "lightkeeper.runplots._write_qc_figure", return_value="qc.png"
            ) as capture:
                report = export_run_figures(folder, {"plan": {"slot_s": 1}})
            self.assertEqual(report["qc_overview"], "qc.png")
            _, intervals, spectral, acquisition, _ = capture.call_args.args
            self.assertLess(intervals[0][1], intervals[1][0])
            np.testing.assert_allclose(spectral[0][1][0], [250, 1, 1, 100 / 7])
            np.testing.assert_allclose(spectral[1][1][0], spectral[0][1][0])
            self.assertTrue(np.isnan(spectral[0][1][1:3, 1:]).all())  # Below floor and saturated.
            self.assertTrue(np.isnan(spectral[0][1][3]).all())  # No matching dark.
            np.testing.assert_array_equal(acquisition[0][1], [[20, 0], [20, 0], [20, 3], [10, 0]])
