"""The debug log ring (lyrat_resend/main/dbg_log.c) beyond ota_test/test_protocol.py: long lines in several records and
their join in udplog.py (goal 092 #002), the ring filling in the middle of a line, a repeated line number, the slot
boundary. The C code through otahost.dll. Made by goal 092.
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ota_test"))
sys.path.insert(0, str(HERE.parent / "bench"))
import otahost as H  # noqa: E402
import udplog  # noqa: E402
import s3  # noqa: E402

HEADER = (HERE.parent.parent / "main" / "dbg_log.h").read_text(encoding="utf-8")
TEXT_MAX = int(re.search(r"#define DBG_TEXT_MAX\s+(\d+)", HEADER).group(1))
CONT = re.search(r'#define DBG_CONT\s+"([^"]*)"', HEADER).group(1)

OLD_STAT = ("stat up=120 eth=ok link=1 ip=10.0.0.7 dsdin rate=16000 peak=812/790 asdout rate=16000 peak=95/2210 "
            "timeouts=0/0 ovf=0/1 send_fail=0/0 sent=7990/7990 streaming=1 pins_ok=1")
NEW_STAT = (OLD_STAT + " uart tx=1 rx=5120 frames=118 bad=0/2 lrelay_lost=0 selftest=pass net=wifi rssi=-61")


def records(ring):
    out = []
    while True:
        data, n = ring.pack()
        if not n:
            return out
        out += udplog.parse_records(data)


def joined(recs):
    j = udplog.Joiner()
    out = []
    for r in recs:
        out += j.feed(r)
    return out + j.flush()


class LongLines(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(CONT, udplog.CONT)
        self.assertGreater(len(NEW_STAT), TEXT_MAX)

    def test_short_line_is_one_record_as_before(self):
        ring = H.Ring(8, 0, 1)
        self.assertEqual(ring.log_long(5, "s3eth stage3 ok"), 1)
        self.assertEqual([r["text"] for r in records(ring)], ["s3eth stage3 ok"])

    def test_exact_slot_boundary(self):
        for n in (TEXT_MAX - 2, TEXT_MAX - 1, TEXT_MAX, TEXT_MAX + 1):
            ring = H.Ring(8, 0, 1)
            line = (("w" * 9 + " ") * (n // 10 + 1))[:n - 1] + "z"     # no space at the end
            k = ring.log_long(1, line)
            recs = records(ring)
            self.assertEqual(k, 1 if n <= TEXT_MAX - 1 else 2, n)
            self.assertTrue(all(len(r["text"]) <= TEXT_MAX - 1 for r in recs))
            self.assertFalse(any(r["text"].endswith("~") for r in recs), n)
            self.assertEqual(joined(recs)[0]["text"], " ".join(line.split()) if k > 1 else line)

    def test_stat_line_arrives_whole(self):
        ring = H.Ring(16, 0, 0x55)
        k = ring.log_long(10, NEW_STAT)
        recs = records(ring)
        self.assertEqual(len(recs), k)
        self.assertGreaterEqual(k, 2)
        self.assertTrue(all(r["text"].startswith(CONT) for r in recs[1:]))
        self.assertEqual([r["seq"] for r in recs], list(range(k)))
        whole = joined(recs)
        self.assertEqual(len(whole), 1)
        self.assertEqual(whole[0]["text"], NEW_STAT)
        st = s3.parse_stat(whole[0]["text"])
        self.assertEqual((st["net"], st["rssi"], st["selftest"], st["pins_ok"], st["bad"]), ("wifi", -61, "pass", 1, [0, 2]))

    def test_no_space_to_cut_at(self):
        ring = H.Ring(16, 0, 1)
        line = "x" * 400
        k = ring.log_long(1, line)
        recs = records(ring)
        self.assertEqual(k, 3)
        self.assertEqual(joined(recs)[0]["text"].replace(" ", ""), line)

    def test_ring_full_in_the_middle_keeps_the_head_and_counts_the_rest(self):
        ring = H.Ring(2, 0, 1)
        ring.log(0, "first")
        k = ring.log_long(1, NEW_STAT)
        self.assertEqual(k, 1)
        info = ring.info()
        self.assertEqual(info["count"], 2)
        self.assertGreaterEqual(info["lost"], 1)
        recs = records(ring)
        self.assertEqual(joined(recs)[-1]["text"], NEW_STAT[:len(recs[-1]["text"])])

    def test_continuation_from_another_boot_or_after_a_gap_is_not_joined(self):
        ring = H.Ring(16, 0, 1)
        ring.log_long(1, NEW_STAT)
        recs = records(ring)
        gap = [recs[0]] + recs[2:]                       # the middle record lost on the way
        self.assertEqual(len(joined(gap)), len(gap))
        other = [recs[0], dict(recs[1], boot=2)]
        self.assertEqual(len(joined(other)), 2)


class OldAndNewStat(unittest.TestCase):
    def test_old_line(self):
        st = s3.parse_stat("I (5) x: " + OLD_STAT)
        self.assertEqual((st["up"], st["eth"], st["link"], st["sent"], st["streaming"], st["pins_ok"]),
                         (120, "ok", 1, [7990, 7990], 1, 1))
        self.assertEqual((st["dsdin_peak"], st["asdout_peak"], st["ovf"]), ([812, 790], [95, 2210], [0, 1]))
        self.assertNotIn("net", st)
        self.assertNotIn("ip", st)

    def test_new_line(self):
        st = s3.parse_stat(NEW_STAT)
        self.assertEqual((st["uart_tx"], st["uart_rx"], st["frames"], st["lrelay_lost"]), (1, 5120, 118, 0))
        self.assertEqual((st["net"], st["rssi"]), ("wifi", -61))

    def test_line_cut_by_an_older_s3(self):
        cut = NEW_STAT[:TEXT_MAX - 2] + "~"
        st = s3.parse_stat(cut)
        self.assertEqual(st["up"], 120)
        self.assertNotIn("rssi", st)

    def test_not_a_stat_line(self):
        self.assertIsNone(s3.parse_stat("I (1) s3eth: wifi ip 10.0.0.7"))


class RepeatedNumbers(unittest.TestCase):
    def test_a_datagram_twice_counts_duplicates_not_lines_lost(self):
        ring = H.Ring(16, 1, 9)
        for i in range(5):
            ring.log(i, "I (%d) BT: %d" % (i, i))
        recs = records(ring)
        t = udplog.Tracker()
        for r in recs + recs:
            t.add(r)
        s = t.summary()["lyrat"]
        self.assertEqual((s["lines"], s["duplicates"], s["lost_way"], s["lost_board"]), (10, 5, 0, 0))


if __name__ == "__main__":
    unittest.main()
