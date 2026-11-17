"""Edge cases of the control UART framing (lyrat_resend/main/ctl_frame.h), the firmware's own C parser through otahost.dll.

Boundaries of the length field, a damaged CRC and header check, garbage before and inside frames, a frame cut by a pause,
a repeated seq. The Python builder of s3_uart.py must give the same bytes as the C one. Made by goal 092.
Needs ota_test/build/otahost.dll (ota_test/build_host.cmd).
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "ota_test"))
sys.path.insert(0, str(TOOLS / "bench"))
import otahost as H  # noqa: E402
import s3_uart  # noqa: E402

HEADER = (TOOLS.parent / "main" / "ctl_frame.h").read_text(encoding="utf-8")


def define(name):
    return int(re.search(r"#define\s+%s\s+(\d+)" % name, HEADER).group(1))


MAX_PAYLOAD = define("CTL_MAX_PAYLOAD")
GAP_MS = define("CTL_GAP_MS")


def feed(parser, data, t=1000):
    return parser.feed(data, t)


class Framing(unittest.TestCase):
    def test_python_builder_matches_c(self):
        for kind, seq, payload in ((1, 0, b""), (2, 0xFFFF, b"\x00ok"), (5, 0x1234, bytes(range(256)) * 4)):
            self.assertEqual(s3_uart.frame(kind, seq, payload), H.ctl_frame(kind, seq, payload))

    def test_empty_and_max_payload(self):
        p = H.CtlParser()
        got = feed(p, H.ctl_frame(1, 7, b"") + H.ctl_frame(2, 8, b"x" * MAX_PAYLOAD))
        self.assertEqual([(k, s, len(b)) for k, s, b in got], [(1, 7, 0), (2, 8, MAX_PAYLOAD)])
        self.assertEqual(p.counts()["frames"], 2)

    def test_payload_over_max_is_a_bad_header(self):
        raw = bytearray(H.ctl_frame(1, 1, b"y" * MAX_PAYLOAD))
        n = MAX_PAYLOAD + 1
        raw[5], raw[6] = n & 0xFF, n >> 8
        raw[7] = raw[2] ^ raw[3] ^ raw[4] ^ raw[5] ^ raw[6]     # a header that checks, with a length too big
        p = H.CtlParser()
        self.assertEqual(feed(p, bytes(raw) + H.ctl_frame(3, 2, b"hb")), [(3, 2, b"hb")])
        self.assertEqual(p.counts()["bad_header"], 1)

    def test_bad_crc_and_bad_header_check(self):
        p = H.CtlParser()
        data = (s3_uart.frame(1, 1, b"a", flip_crc=True) + s3_uart.frame(1, 2, b"b", flip_header=True)
                + s3_uart.frame(1, 3, b"c"))
        self.assertEqual(feed(p, data), [(1, 3, b"c")])
        c = p.counts()
        self.assertEqual((c["bad_crc"], c["bad_header"], c["frames"]), (1, 1, 1))

    def test_every_single_bit_flip_is_refused(self):
        good = H.ctl_frame(2, 0x0102, b"\x00status text")
        for i in range(2, len(good)):           # the magic bytes only resync, the rest is covered by the checks
            for bit in range(8):
                raw = bytearray(good)
                raw[i] ^= 1 << bit
                p = H.CtlParser()
                self.assertEqual(feed(p, bytes(raw)), [], "flip at byte %d bit %d passed" % (i, bit))

    def test_garbage_between_frames(self):
        p = H.CtlParser()
        noise = b"\x00\xff\xa5\x00boot garbage\xa5\xa5"
        got = feed(p, noise + H.ctl_frame(3, 1, b"1") + noise + H.ctl_frame(3, 2, b"2"))
        self.assertEqual([s for _, s, _ in got], [1, 2])
        self.assertGreater(p.counts()["noise"], 0)

    def test_repeated_first_magic_byte_still_starts_a_frame(self):
        p = H.CtlParser()
        self.assertEqual(feed(p, b"\xa5\xa5\xa5" + H.ctl_frame(1, 9, b"z")), [(1, 9, b"z")])

    def test_frame_right_after_a_lone_magic_pair_is_found(self):
        # The LyraT prints garbage at its start; "A5 5A" in it must not cost the real frame that follows.
        p = H.CtlParser()
        self.assertEqual(feed(p, b"\xa5\x5a" + H.ctl_frame(1, 5, b"cmd")), [(1, 5, b"cmd")])

    def test_frame_inside_a_false_header_is_found(self):
        # A false start whose header fails its check, with the real frame beginning inside those header bytes.
        p = H.CtlParser()
        self.assertEqual(feed(p, b"\xa5\x5a\x01\x02" + H.ctl_frame(2, 6, b"\x00ok")), [(2, 6, b"\x00ok")])

    def test_pause_drops_a_cut_frame_only(self):
        p = H.CtlParser()
        whole = H.ctl_frame(1, 1, b"first")
        self.assertEqual(p.feed(whole[:6], 1000), [])
        got = p.feed(H.ctl_frame(1, 2, b"second"), 1000 + GAP_MS + 1)
        self.assertEqual(got, [(1, 2, b"second")])
        self.assertEqual(p.counts()["truncated"], 1)

    def test_pause_just_under_the_gap_keeps_the_frame(self):
        p = H.CtlParser()
        whole = H.ctl_frame(1, 1, b"slow")
        self.assertEqual(p.feed(whole[:9], 1000), [])
        self.assertEqual(p.feed(whole[9:], 1000 + GAP_MS), [(1, 1, b"slow")])

    def test_clock_wrap(self):
        p = H.CtlParser()
        whole = H.ctl_frame(1, 3, b"wrap")
        self.assertEqual(p.feed(whole[:5], 0xFFFFFFF0), [])
        self.assertEqual(p.feed(whole[5:], 0x00000010), [(1, 3, b"wrap")])

    def test_repeated_seq_is_delivered_as_is(self):
        # The parser does not drop repeats: the LyraT console answers a repeated UC seq from its copy (docs/S3_UART.md).
        p = H.CtlParser()
        f = H.ctl_frame(1, 42, b"tone 1")
        self.assertEqual(feed(p, f + f), [(1, 42, b"tone 1")] * 2)


if __name__ == "__main__":
    unittest.main()
