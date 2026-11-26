"""Remembered recording folders must not follow a drive letter to a different volume."""
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lightkeeper import locations, procs


class TestRunLocations(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first, self.second, self.third = [self.root / label for label in ("first", "second", "third")]
        self.dirs = {}

        def volume_location(path):
            for root, (guid, serial) in self.dirs.items():
                if path.is_relative_to(root):
                    return guid, serial, PureWindowsPath(path.relative_to(root))
            return None

        def volume_mounts(guid):
            return [str(p) for p, ids in self.dirs.items() if ids[0] == guid] if guid else []

        def serial_mounts(serial):
            return [str(p) for p, ids in self.dirs.items() if ids[1] == serial] if serial else []

        for name, fn in (("_volume_location", volume_location), ("_volume_mounts", volume_mounts),
                         ("_serial_mounts", serial_mounts)):
            mock = patch.object(locations, name, side_effect=fn)
            mock.start()
            self.addCleanup(mock.stop)

    def test_remount_keeps_relative_path_and_rejects_wrong_original_volume(self):
        self.dirs[self.first] = ("expected-guid", "11111111")
        saved = locations.remember_path(self.first / "LightKeeper" / "run")
        self.dirs[self.first] = ("different-guid", "11111111")  # A cloned serial alone is insufficient.
        self.assertIsNone(locations.resolve_path(saved))
        self.dirs[self.second] = ("expected-guid", "11111111")
        remounted = self.second / "LightKeeper" / "run"
        self.assertEqual(locations.resolve_path(saved), remounted)
        self.assertEqual(locations.location_key(saved), locations.location_key(locations.remember_path(remounted)))

    def test_serial_only_remount_has_to_be_unambiguous(self):
        self.dirs[self.first] = ("", "11111111")
        saved = locations.remember_path(self.first / "new-run-folder")
        self.dirs[self.first] = ("", "22222222")
        self.dirs[self.second] = ("", "11111111")
        self.assertEqual(locations.resolve_path(saved), self.second / "new-run-folder")
        self.dirs[self.third] = ("", "11111111")
        self.assertIsNone(locations.resolve_path(saved))
        self.dirs[self.first] = ("", "11111111")
        self.assertIsNone(locations.resolve_path(saved))  # The old letter does not break a serial collision.

    def test_registry_keeps_same_name_runs_separate_and_records_an_offline_run(self):
        self.dirs[self.first] = ("guid-one", "11111111")
        self.dirs[self.second] = ("guid-two", "22222222")
        one, two = self.first / "day_v01", self.second / "day_v01"
        with patch.dict("os.environ", {"LIGHTKEEPER_HOME": str(self.root / "app")}):
            for run in (one, two):
                run.mkdir(parents=True)
                procs.write_json_atomic(run / "run_state.json", {"serial": "device", "status": "open"})
                procs.register_open_run(run, "device", recovery={"enabled": True})
            self.assertEqual({r["run_dir"] for r in procs.open_runs()}, {str(one), str(two)})

            del self.dirs[self.first]
            self.assertEqual([r["run_dir"] for r in procs.open_runs()], [str(two)])
            offline = [r for r in procs.open_runs(include_unavailable=True) if r["state"] is None]
            self.assertEqual([r["run_dir"] for r in offline], [str(one)])
            self.assertTrue(offline[0]["recovery"]["enabled"])

            self.first.rename(self.third)
            self.dirs[self.third] = ("guid-one", "11111111")
            relocated = self.third / "day_v01"
            self.assertEqual({r["run_dir"] for r in procs.open_runs()}, {str(two), str(relocated)})
            procs.unregister_open_run(relocated)
            self.assertEqual([r["run_dir"] for r in procs.open_runs()], [str(two)])

    def test_registry_migrates_identity_and_closes_after_volume_loss(self):
        self.dirs[self.first] = ("", "11111111")
        run = self.first / "day_v01"
        with patch.dict("os.environ", {"LIGHTKEEPER_HOME": str(self.root / "app")}):
            first = procs.register_open_run(run, "device")
            self.dirs[self.first] = ("guid-one", "11111111")
            latest = procs.register_open_run(run, "device")
            self.assertNotEqual(first, latest)
            self.assertFalse(first.exists())
            self.assertTrue(latest.exists())

            del self.dirs[self.first]
            procs.unregister_open_run(run, registration=latest)
            self.assertEqual(list(latest.parent.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
