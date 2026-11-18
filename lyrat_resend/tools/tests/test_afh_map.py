"""The AFH map of the LyraT from the S3's view of Wi-Fi (lyrat_resend/main/afh_map.c, goal 106), the firmware's own C
through otahost.dll: the band of a Wi-Fi channel, the map of a report (order, floor, hysteresis, scan age), the report
in the heartbeat ack (old S3s, broken bytes), the scan reduction of the S3, and the policy (no S3, staleness, no change
during the headset channel, link up, commands). Needs ota_test/build/otahost.dll (ota_test/build_host.cmd).
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ota_test"))
import otahost as H  # noqa: E402

HEADER = (HERE.parent.parent / "main" / "afh_map.h").read_text(encoding="utf-8")
MIN_KEEP = int(re.search(r"#define AFH_MIN_KEEP\s+(\d+)", HEADER).group(1))
STALE_MS = 30 * 60 * 1000
M_100 = "0000c0ffffff0100007f"      # goal 100's mask without Wi-Fi 1 and 11 (34 channels)


def channels(m):
    return [k for k in range(79) if (m[k // 8] >> (k % 8)) & 1]


class Span(unittest.TestCase):
    def test_20mhz(self):
        self.assertEqual(H.afh_span(1), (0, 21))
        self.assertEqual(H.afh_span(6), (24, 46))          # 2437 +- 11
        self.assertEqual(H.afh_span(11), (49, 71))
        self.assertEqual(H.afh_span(13), (59, 78))         # 2472 +- 11 ends past channel 78
        self.assertEqual(H.afh_span(14), (71, 78))         # 2484

    def test_40mhz(self):
        self.assertEqual(H.afh_span(1, 1, True), (0, 41))
        self.assertEqual(H.afh_span(11, 2, True), (29, 71))
        self.assertEqual(H.afh_span(1, 1, False), (0, 21))  # secondary known, own bandwidth 20

    def test_out_of_range(self):
        self.assertIsNone(H.afh_span(0))
        self.assertIsNone(H.afh_span(15))


class Hex(unittest.TestCase):
    def test_all_and_m(self):
        self.assertEqual(H.afh_hex(H.AFH_ALL), "ffffffffffffffffff7f")
        self.assertEqual(H.afh_count(H.AFH_ALL), 79)
        m = H.afh_from_hex(M_100)
        self.assertEqual(H.afh_count(m), 34)
        self.assertEqual(H.afh_hex(m), M_100)

    def test_bad_text(self):
        self.assertIsNone(H.afh_from_hex("ff"))
        self.assertIsNone(H.afh_from_hex("zz" * 10))
        self.assertEqual(H.afh_hex(H.afh_from_hex("ff" * 10)), "ffffffffffffffffff7f")   # no channel 79


class Build(unittest.TestCase):
    def test_nothing_known_is_all(self):
        m, n, excl = H.afh_build(H.afh_report())
        self.assertEqual((m, n, excl), (H.AFH_ALL, 79, ""))

    def test_ap_1_and_neighbour_11_is_the_mask_of_100(self):
        r = H.afh_report(ap=1, rssi=-49, age=30, nets=[(1, 0, -47), (11, 0, -49), (6, 0, -55)])
        m, n, excl = H.afh_build(r)
        self.assertEqual(H.afh_hex(m), M_100)
        self.assertEqual(n, 34)
        self.assertEqual(excl, "ap1,11:-49")      # 6 would leave 11 channels: skipped

    def test_stronger_6_is_taken_instead_of_11(self):
        r = H.afh_report(ap=1, age=30, nets=[(6, 0, -50), (11, 0, -55)])
        m, n, excl = H.afh_build(r)
        self.assertEqual(excl, "ap1,6:-50")
        self.assertEqual(channels(m), list(range(22, 24)) + list(range(47, 79)))
        self.assertGreaterEqual(n, MIN_KEEP)

    def test_ap_always_even_weak(self):
        m, n, excl = H.afh_build(H.afh_report(ap=6, rssi=-90))
        self.assertEqual(excl, "ap6")
        self.assertEqual(n, 79 - 23)

    def test_ap_40_takes_42_and_nothing_else_fits(self):
        r = H.afh_report(ap=1, second=1, flags=3, age=5, nets=[(11, 0, -40)])
        m, n, excl = H.afh_build(r)
        self.assertEqual(excl, "ap1+")
        self.assertEqual(n, 79 - 42)

    def test_weak_networks_ignored(self):
        r = H.afh_report(ap=1, age=5, nets=[(11, 0, -71), (6, 0, -80)])
        self.assertEqual(H.afh_build(r)[2], "ap1")
        r = H.afh_report(ap=1, age=5, nets=[(11, 0, -70)])
        self.assertEqual(H.afh_build(r)[2], "ap1,11:-70")

    def test_hysteresis(self):
        r = H.afh_report(ap=1, age=5, nets=[(11, 0, -73)])
        self.assertEqual(H.afh_build(r)[2], "ap1")                          # new: needs -70
        held = H.afh_from_hex(M_100)
        self.assertEqual(H.afh_build(r, prev=held)[2], "ap1,11:-73")         # held: until -76
        r = H.afh_report(ap=1, age=5, nets=[(11, 0, -77)])
        self.assertEqual(H.afh_build(r, prev=held)[2], "ap1")

    def test_scan_not_usable(self):
        r = H.afh_report(ap=1, age=5, nets=[(11, 0, -40)])
        self.assertEqual(H.afh_build(r, scan_ok=False)[2], "ap1")

    def test_overlap_with_ap_adds_nothing_to_the_text(self):
        r = H.afh_report(ap=1, age=5, nets=[(1, 0, -40), (2, 0, -45)])
        m, n, excl = H.afh_build(r)
        self.assertEqual(excl, "ap1,2:-45")          # channel 1 of the scan is the AP itself
        self.assertEqual(n, 79 - 27)

    def test_floor_never_broken(self):
        for ap in range(0, 15):
            for second in (0, 1, 2):
                r = H.afh_report(ap=ap, second=second, flags=3, age=1,
                                 nets=[(c, 0, -40 - c) for c in (1, 3, 5, 7, 9, 11, 13, 14)])
                n = H.afh_build(r)[1]
                self.assertGreaterEqual(n, MIN_KEEP)       # the AP alone leaves at least 37
                self.assertGreaterEqual(n, 20)


class Report(unittest.TestCase):
    def test_round_trip(self):
        r = H.afh_report(ap=11, second=2, rssi=-61, flags=3, age=600, gen=7,
                         nets=[(1, 0, -47), (11, 2, -49), (6, 0, -55)])
        ack = H.afh_ack(r)
        self.assertEqual(len(ack), 12 + 9)
        got, p = H.afh_parse(ack)
        self.assertEqual(got, 1)
        self.assertEqual((p.gen, p.ap_primary, p.ap_second, p.ap_rssi, p.flags, p.scan_age_s, p.n),
                         (7, 11, 2, -61, 3, 600, 3))
        self.assertEqual([(p.nets[i].primary, p.nets[i].second, p.nets[i].rssi) for i in range(3)],
                         [(1, 0, -47), (11, 2, -49), (6, 0, -55)])

    def test_old_s3(self):
        self.assertEqual(H.afh_parse(b"")[0], 0)            # before goal 090
        self.assertEqual(H.afh_parse(b"\x01\x03")[0], 0)    # goal 090..105

    def test_broken(self):
        good = H.afh_ack(H.afh_report(ap=1, age=5, nets=[(11, 0, -49)]))
        self.assertEqual(H.afh_parse(good)[0], 1)
        self.assertEqual(H.afh_parse(good[:-1])[0], -1)                       # cut
        self.assertEqual(H.afh_parse(good[:2] + b"\x02" + good[3:])[0], -1)  # version
        bad_n = bytearray(good)
        bad_n[11] = 9
        self.assertEqual(H.afh_parse(bytes(bad_n) + bytes(30))[0], -1)        # too many networks
        bad_ch = bytearray(good)
        bad_ch[12] = 15
        self.assertEqual(H.afh_parse(bytes(bad_ch))[0], -1)
        bad_ap = bytearray(good)
        bad_ap[4] = 200
        self.assertEqual(H.afh_parse(bytes(bad_ap))[0], -1)

    def test_encode_caps_networks(self):
        r = H.afh_report(ap=1, nets=[(c, 0, -50) for c in range(1, 9)])
        r.n = 12
        self.assertEqual(len(H.afh_ack(r)), 12 + 3 * 8)


class Reduce(unittest.TestCase):
    def test_strongest_per_channel_sorted(self):
        got = H.afh_reduce([(6, 0, -60), (1, 0, -50), (6, 0, -54), (11, 0, -49), (1, 1, -70), (0, 0, -10), (3, 5, -1)])
        self.assertEqual(got, [(11, 0, -49), (1, 0, -50), (6, 0, -54), (1, 1, -70)])

    def test_cap(self):
        self.assertEqual(len(H.afh_reduce([(c, 0, -40 - c) for c in range(1, 14)], 8)), 8)
        self.assertEqual(H.afh_reduce([]), [])


def ack(ap=1, nets=((11, 0, -49),), age=5, gen=1, **kw):
    return H.afh_ack(H.afh_report(ap=ap, age=age, nets=list(nets), gen=gen, **kw))


class Policy(unittest.TestCase):
    def test_without_s3_nothing_happens(self):
        c = H.CAfhCtl("auto")
        self.assertIsNone(c.poll(1000))
        c.slc()
        self.assertIsNone(c.poll(2000))            # link up, all channels: no call, as before this goal
        self.assertEqual(c.ack(b"\x01\x03", 3000), 0)
        self.assertIsNone(c.poll(3000))
        self.assertEqual(c.info()["applies"], 0)

    def test_mode_all_ignores_reports(self):
        c = H.CAfhCtl("all")
        c.ack(ack(), 1000)
        self.assertIsNone(c.poll(1000))
        self.assertEqual(c.target(1000), (H.AFH_ALL, "none"))

    def test_auto_applies_once(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        m, src, why = c.poll(1000)
        self.assertEqual((H.afh_hex(m), src, why), (M_100, "s3", "s3"))
        for t in range(2000, 20000, 1000):
            c.ack(ack(), t)
            self.assertIsNone(c.poll(t))
        self.assertEqual(c.info()["applies"], 1)

    def test_no_change_during_sco(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        self.assertIsNone(c.poll(1000, sco=True))
        self.assertTrue(c.info()["deferred"])
        self.assertIsNone(c.poll(2000, sco=True))
        self.assertEqual(c.info()["deferrals"], 1)
        m, src, why = c.poll(3000, sco=False)
        self.assertEqual((H.afh_hex(m), why), (M_100, "sco_end"))

    def test_change_reverted_during_sco_is_no_change(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        c.poll(1000)
        c.ack(ack(ap=6, nets=()), 2000)
        self.assertIsNone(c.poll(2000, sco=True))
        c.ack(ack(), 3000)
        self.assertIsNone(c.poll(3000, sco=True))
        self.assertIsNone(c.poll(4000))
        self.assertFalse(c.info()["deferred"])

    def test_ap_channel_change(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        c.poll(1000)
        c.ack(ack(ap=6, nets=(), gen=2), 2000)
        m, src, why = c.poll(2000)
        self.assertEqual(channels(m), list(range(0, 24)) + list(range(47, 79)))
        self.assertEqual(why, "s3")

    def test_stale_back_to_all(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        c.poll(1000)
        self.assertIsNone(c.poll(1000 + STALE_MS))          # 30 min: the last map holds
        m, src, why = c.poll(1001 + STALE_MS)
        self.assertEqual((m, src, why), (H.AFH_ALL, "none", "stale"))
        c.ack(ack(), 2 * STALE_MS)                          # the S3 is back
        self.assertEqual(c.poll(2 * STALE_MS)[2], "s3")

    def test_clock_wrap(self):
        c = H.CAfhCtl("auto")
        t0 = 0xFFFFFF00
        c.ack(ack(), t0)
        self.assertEqual(c.poll(t0)[1], "s3")
        self.assertIsNone(c.poll(t0 + 5000))                # past the wrap, not stale

    def test_old_scan_is_dropped(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(age=3500), 0)
        self.assertEqual(H.afh_hex(c.poll(0)[0]), M_100)
        m, src, why = c.poll(101 * 1000)                    # scan now 3601 s old: only the AP
        self.assertEqual(channels(m), list(range(22, 79)))
        c2 = H.CAfhCtl("auto")
        c2.ack(ack(age=0xFFFF), 0)                          # no scan yet
        self.assertEqual(channels(c2.poll(0)[0]), list(range(22, 79)))

    def test_link_up_gives_again(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        c.poll(1000)
        c.slc()
        self.assertIsNone(c.poll(2000, sco=True))
        m, src, why = c.poll(3000)
        self.assertEqual((H.afh_hex(m), why), (M_100, "slc"))
        self.assertIsNone(c.poll(4000))

    def test_commands(self):
        c = H.CAfhCtl("all")
        c.ack(ack(), 1000)
        c.set_mode("auto")
        m, src, why = c.poll(1000)
        self.assertEqual((H.afh_hex(m), src, why), (M_100, "s3", "cmd"))
        c.set_mode("all")
        self.assertEqual(c.poll(2000), (H.AFH_ALL, "none", "cmd"))
        c.set_mode("all")
        self.assertEqual(c.poll(3000), (H.AFH_ALL, "none", "cmd"))     # a command always reaches the controller
        fixed = H.afh_from_hex("0000c0ffffffffffff7f")
        c.set_mode("fixed", fixed)
        self.assertEqual(c.poll(4000), (fixed, "cmd", "cmd"))
        c.ack(ack(ap=6, nets=()), 5000)
        self.assertIsNone(c.poll(5000))                                  # fixed ignores the S3

    def test_broken_ack_keeps_last_report(self):
        c = H.CAfhCtl("auto")
        c.ack(ack(), 1000)
        c.poll(1000)
        self.assertEqual(c.ack(ack()[:-2], 2000), -1)
        self.assertEqual(c.info()["bad"], 1)
        self.assertIsNone(c.poll(2000))
        self.assertEqual(c.info()["rep_ms"], 1000)


class Events(unittest.TestCase):
    """benchd's event index knows the LyraT's map line and the S3's scan line (bench_events.py)."""

    def test_lines(self):
        sys.path.insert(0, str(HERE.parent / "bench"))
        import bench_events
        line = "AFH map n=34 map=%s src=s3 mode=auto ap=1 excl=ap1,11:-49 why=s3" % M_100
        hits = [r for r in bench_events.RULES if r[2].search(line)]
        self.assertEqual([(h[0], h[1]) for h in hits], [("afh_map", "lyrat")])
        hits = [r for r in bench_events.RULES if r[2].search("wifi scan aps=9 nets=5 top=1:-47,11:-49,6:-55")]
        self.assertEqual([(h[0], h[1]) for h in hits], [("wifi_scan", "s3")])
        main = (HERE.parent.parent / "main" / "main.c").read_text(encoding="utf-8")
        self.assertIn('"AFH map n=%d map=%s src=%s mode=%s ap=%u excl=%s why=%s"', main)


if __name__ == "__main__":
    unittest.main()
