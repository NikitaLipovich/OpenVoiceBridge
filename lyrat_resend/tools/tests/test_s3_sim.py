"""The simulator against the firmware source: what s3_sim.py says on the network must be what s3_eth_bridge/main writes.

Numbers, JSON keys, the stream header and the stat line are parsed from the C files (s3_sim.facts()), not copied by hand.
Made by goal 092.
"""
import json
import re
import socket
import struct
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bench"))
import s3_sim  # noqa: E402
import simproc  # noqa: E402
import s3  # noqa: E402

F, D = s3_sim.F, s3_sim.D
S3_OTA = s3_sim.REPO / "s3_eth_bridge" / "main" / "s3_ota.c"


def ota_query_keys():
    src = S3_OTA.read_text(encoding="utf-8")
    body = src[src.index("static int query_json"):src.index("static void udp_task")]
    body = body[body.index("int n = snprintf(out, cap,"):]
    return s3_sim.json_keys(body)


class Facts(unittest.TestCase):
    """The parse of the C source itself: if the firmware changes shape, these say so before the simulator lies."""

    def test_numbers(self):
        self.assertEqual((D["PORT_CALL"], D["PORT_MIC"], D["PORT_REQUEST"]), (5004, 5005, 5006))
        self.assertEqual(D["HEADER_BYTES"], 24)
        self.assertEqual(D["S3_NET_ETH"], 0, "the S3 must build for Wi-Fi by default")

    def test_header_layout_covers_every_byte_once(self):
        covered = []
        for at, (size, _) in F["layout"].items():
            covered += range(at, at + size)
        self.assertEqual(covered, list(range(D["HEADER_BYTES"])))

    def test_simulator_header_matches_the_c_layout(self):
        fmt = "<2sBBIIHBBII"        # the one s3_sim.Sim.packet() and udp_record._TapStream use
        self.assertEqual(struct.calcsize(fmt), D["HEADER_BYTES"])
        offsets, at = [], 0
        for code in ["B", "B"] + list(fmt[3:]):     # "2s" is the two magic bytes, one entry each in the C
            size = struct.calcsize("<" + code)
            offsets.append((at, size))
            at += size
        self.assertEqual(offsets, [(k, v[0]) for k, v in F["layout"].items()])
        names = [v[1] for v in F["layout"].values()]
        self.assertEqual(names[4:], ["seq", "first", "s_boot_id", "(uint8_t)channels", "(uint8_t)(frames / 8)",
                                     "overflows", "send_fail"])


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()
        cls.port = cls.sim.ports["tap_port"]
        cls.socks = {}
        for name, key in (("call", "call_port"), ("mic", "mic_port")):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("0.0.0.0", cls.sim.ports[key]))
            s.settimeout(0.5)
            cls.socks[name] = s
        time.sleep(1.5)     # a few heartbeats: the S3 TX follows the LyraT

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()
        for s in cls.socks.values():
            s.close()

    def drain(self):
        for s in self.socks.values():
            s.settimeout(0.05)
            try:
                while True:
                    s.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                pass
            s.settimeout(0.5)

    def packets(self, name, n):
        out = []
        while len(out) < n:
            out.append(self.socks[name].recvfrom(4096)[0])
        return out

    def tap(self, text):
        got = simproc.ask(self.port, text)
        return json.loads(got[0]) if got else None

    def test_tap_reply_keys_from_c(self):
        r = self.tap("TAP1 L LR")
        self.assertEqual(s3_sim.flat_keys(r), F["tap_keys"])
        self.assertEqual(r["ok"], 1)
        self.tap("TAP0")

    def test_uq_keys_from_c(self):
        r = json.loads(simproc.ask(self.port, "UQ")[0])
        self.assertEqual(s3_sim.flat_keys(r), F["uq_keys"])

    def test_ota_query_keys_from_c(self):
        sys.path.insert(0, str(HERE.parent / "bench"))
        import ota
        link = ota.Link("127.0.0.1", self.sim.ports["port"])
        try:
            q = link.query()
        finally:
            link.close()
        # The C writes the object of one receiver in a loop over both targets: its keys appear once in the source.
        self.assertEqual(len(q["rx"]), 2)
        one = dict(q, rx=q["rx"][:1])
        self.assertIsNone(q["last_invalid"])        # its keys come from another snprintf, not part of the format
        self.assertEqual(s3_sim.flat_keys(one), ota_query_keys())

    def test_log_reply_keys_from_c(self):
        got = simproc.ask(self.sim.ports["log_port"], "LOG1 3 3", want=5)
        line = next(g for g in got if g.startswith(b"LOG "))
        self.assertEqual(s3_sim.flat_keys(json.loads(line.split(b" ", 2)[2])), F["log_keys"])
        simproc.ask(self.sim.ports["log_port"], "LOG0", want=20, timeout=0.3)

    def test_stream_packets(self):
        self.drain()
        self.tap("TAP1 L LR")
        call = [p for p in self.packets("call", 6) if p[:2] == b"S3"]
        mic = [p for p in self.packets("mic", 6) if p[:2] == b"S3"]
        self.tap("TAP0")
        for pk, stream, ch in ((call, 0, 1), (mic, 1, 2)):
            heads = [struct.unpack_from("<2sBBIIHBBII", p) for p in pk]
            for h, p in zip(heads, pk):
                self.assertEqual(h[1:3], (D["PACKET_VERSION"], stream))
                self.assertEqual(h[6], ch)
                self.assertEqual(h[7], D["DMA_FRAMES"] // 8)
                self.assertEqual(len(p), D["HEADER_BYTES"] + D["DMA_FRAMES"] * ch * 2)
            seqs = [h[3] for h in heads]
            self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
            firsts = [h[4] for h in heads]
            self.assertTrue(all(b - a == D["DMA_FRAMES"] for a, b in zip(firsts, firsts[1:])))

    def test_tap1_bad_fields_and_unknown_word(self):
        self.assertEqual(self.tap("TAP1 L")["ok"], 0)        # fields == 2 in the C sscanf: refused
        self.assertEqual(self.tap("TAP1 X LR")["ok"], 0)
        self.assertEqual(simproc.ask(self.port, "HELLO", timeout=0.3), [])    # not addressed to this board

    def test_tap0_stops_and_lease_ends(self):
        self.tap("TAP1")
        self.packets("call", 2)
        self.tap("TAP0")
        time.sleep(0.2)
        self.drain()
        with self.assertRaises(socket.timeout):
            self.socks["call"].recvfrom(4096)
        self.tap("TAP1 LR LR")
        self.packets("call", 2)
        time.sleep(D["LEASE_MS"] / 1000.0 + 0.3)
        self.drain()
        with self.assertRaises(socket.timeout):
            self.socks["call"].recvfrom(4096)

    def test_uc_runs_once_per_seq(self):
        a = simproc.ask(self.port, "UC 777 tone 1", timeout=1.0)
        b = simproc.ask(self.port, "UC 777 tone 1", timeout=1.0)
        self.assertEqual(a, b)
        self.assertTrue(a[0].startswith(b"UR 777 0\n"))
        uq = json.loads(simproc.ask(self.port, "UQ")[0])
        self.assertGreaterEqual(uq["replies"], 2)

    def test_uc_bad_and_refused_without_tx(self):
        self.assertEqual(simproc.ask(self.port, "UC x tone 1")[0], b"UR 0 2\nbad request")
        self.assertEqual(simproc.ask(self.port, "UC 70000 tone 1")[0], b"UR 0 2\nbad request")
        self.assertEqual(simproc.ask(self.port, "UT 0")[0], b"UT 0")
        self.assertEqual(simproc.ask(self.port, "UC 5 tone 1")[0], b"UR 5 3\nno_tx")
        self.assertEqual(simproc.ask(self.port, "UW a55a")[0], b"UW -1")
        self.assertEqual(simproc.ask(self.port, "UT A")[0], b"UT 2")
        self.assertEqual(simproc.ask(self.port, "UW a55a")[0], b"UW 2")

    def test_ub_range(self):
        self.assertEqual(simproc.ask(self.port, "UB 9600")[0], b"UB 9600")
        self.assertEqual(simproc.ask(self.port, "UB 5")[0], b"UB 9600")
        self.assertEqual(simproc.ask(self.port, "UB 115200")[0], b"UB 115200")

    def test_stat_line_is_the_firmware_format(self):
        line = Live.sim_stat_line()
        self.assertEqual(re.findall(r"(\w+)=", line), F["stat_keys"])
        self.assertNotIn("~", line)
        st = s3.parse_stat(line)
        self.assertIsNotNone(st)
        self.assertEqual(st.get("net"), "wifi")

    @staticmethod
    def sim_stat_line():
        """A stat line through the log port, as udplog.py gets it."""
        sys.path.insert(0, str(HERE.parent / "bench"))
        import udplog
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(0.5)
        end = time.monotonic() + D["STATUS_MS"] / 1000.0 * 3
        joiner = udplog.Joiner()
        try:
            s.sendto(b"LOG1 3 3", ("127.0.0.1", udplog.LOG_PORT))
            while time.monotonic() < end:
                try:
                    data = s.recvfrom(4096)[0]
                except (socket.timeout, ConnectionResetError):
                    continue
                for rec in udplog.parse_datagram(data) or []:
                    for whole in joiner.feed(rec):
                        if whole["text"].startswith("stat up="):
                            return whole["text"]
        finally:
            s.sendto(b"LOG0", ("127.0.0.1", udplog.LOG_PORT))
            s.close()
        raise AssertionError("no stat line in %.0f s" % (D["STATUS_MS"] / 1000.0 * 3))


if __name__ == "__main__":
    unittest.main()
