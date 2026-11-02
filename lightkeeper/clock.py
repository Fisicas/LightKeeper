"""PC clock quality without admin rights (0.3.1): one SNTP query (UDP 123, standard library only).
Never raises: a failed query returns ok=False with the reason, and the run carries on."""
from __future__ import annotations

import socket
import struct
import time

NTP_HOST = "pool.ntp.org"
NTP_EPOCH = 2208988800           # 1900-01-01 to 1970-01-01, s
WARN_OFFSET_S = 1.0


def _ts(b: bytes) -> float:
    sec, frac = struct.unpack("!II", b)
    return sec - NTP_EPOCH + frac / 2 ** 32


def sntp_query(host: str = NTP_HOST, timeout: float = 2.0, sock_factory=socket.socket) -> dict:
    """offset_s = server - PC clock (positive: the PC is behind); rtt_s = network round trip."""
    try:
        with sock_factory(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            pkt = bytearray(48); pkt[0] = 0x23            # LI 0, version 4, mode 3 (client)
            t1 = time.time()
            s.sendto(bytes(pkt), (host, 123))
            data, _ = s.recvfrom(512)
            t4 = time.time()
        if len(data) < 48:
            raise ValueError(f"short reply ({len(data)} bytes)")
        t2, t3 = _ts(data[32:40]), _ts(data[40:48])
        return dict(ok=True, server=host, offset_s=((t2 - t1) + (t3 - t4)) / 2, rtt_s=(t4 - t1) - (t3 - t2),
                    stratum=data[1], pc_time=t4)
    except Exception as e:
        return dict(ok=False, server=host, error=repr(e))


def w32time_running(status: str) -> bool:
    s = status.lower()
    return not ("0x80070426" in s or "has not been started" in s or s.startswith("unavailable"))
