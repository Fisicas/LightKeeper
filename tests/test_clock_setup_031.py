r"""0.3.1: SNTP clock check with a mocked socket, and the structured setup fields. Run:
   python -X utf8 -m unittest tests.test_clock_setup_031 -v
Set LK_REAL_NTP=1 to also make one real query to pool.ntp.org."""
import os
import socket
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lightkeeper import clock  # noqa: E402
from lightkeeper.schedule import SETUP_FIELDS, RunPlan  # noqa: E402


def ntp_bytes(t):
    return struct.pack("!II", int(t) + clock.NTP_EPOCH, int((t % 1) * 2 ** 32))


class FakeSock:
    """Server clock runs `ahead` s ahead of the PC; each leg of the trip takes 0.05 s (simulated, no sleep)."""
    def __init__(self, ahead=None, fail=None):
        self.ahead, self.fail = ahead, fail

    def __call__(self, *a):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendto(self, pkt, addr):
        assert addr[1] == 123 and len(pkt) == 48 and pkt[0] == 0x23

    def recvfrom(self, n):
        if self.fail:
            raise self.fail
        now = time.time()
        t2 = now + self.ahead; t3 = t2 + 0.001
        return bytes([0x24, 2]) + bytes(30) + ntp_bytes(t2) + ntp_bytes(t3), ("pool", 123)


class TestClock(unittest.TestCase):
    def test_offset_from_mocked_server(self):
        r = clock.sntp_query(sock_factory=FakeSock(ahead=2.5))
        self.assertTrue(r["ok"]); self.assertAlmostEqual(r["offset_s"], 2.5, delta=0.01); self.assertLess(abs(r["rtt_s"]), 0.01)
        self.assertEqual(r["stratum"], 2)

    def test_network_failure_never_raises(self):
        r = clock.sntp_query(sock_factory=FakeSock(fail=socket.timeout("timed out")))
        self.assertFalse(r["ok"]); self.assertIn("timed out", r["error"])

    def test_w32time_status(self):
        self.assertFalse(clock.w32time_running("The service has not been started. (0x80070426)"))
        self.assertTrue(clock.w32time_running("Leap Indicator: 0(no warning)\nSource: time.windows.com"))

    @unittest.skipUnless(os.environ.get("LK_REAL_NTP"), "set LK_REAL_NTP=1 for one real query")
    def test_real_query(self):
        r = clock.sntp_query(); print(f"\n  {r}"); self.assertTrue(r["ok"], r)


class TestSetupFields(unittest.TestCase):
    def test_round_trip(self):
        p = RunPlan(fiber_core_um=50, diffuser_id="PS diffuser used for 6 Oct response", view_az_deg=210, view_el_deg=0,
                    window_state="closed", setup_photo=r"C:\x\setup.jpg")
        q = RunPlan.from_dict(p.to_dict())
        self.assertEqual(p, q)
        self.assertEqual({k: getattr(q, k) for k in SETUP_FIELDS}["diffuser_thickness_mm"], None)

    def test_old_plan_without_fields_loads(self):
        old = RunPlan().to_dict()
        for k in SETUP_FIELDS:
            old.pop(k)
        old["some_future_field"] = 1
        q = RunPlan.from_dict(old)
        self.assertEqual((q.diffuser_id, q.view_az_deg), ("", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
