"""The LyraT's two modes, phone and PC (goal 102): the C rules of main/hf_mode.c through otahost.dll, the scenarios of
the bench on the model of tests/hfp_sim.py, the phone mode against the logic of phone-bringup, and the log reader
tools/bench/hfp_states.py on the logs those scenarios print. Fake addresses only.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ota_test"))
sys.path.insert(0, str(HERE.parent / "bench"))
import hfp_sim as S  # noqa: E402
import hfp_states  # noqa: E402
import otahost as H  # noqa: E402

MAIN = HERE.parent.parent / "main"
SRC = (MAIN / "main.c").read_text(encoding="utf-8")
PHONE = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x01])
PC = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x02])
OTHER = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x03])
NEW_PC = bytes([0x02, 0x11, 0x22, 0x33, 0x44, 0x04])


def summary(world):
    return hfp_states.summarize(hfp_states.parse(world.lyrat.log))


# --- the rules -----------------------------------------------------------------------------------------------------------

class Rules(unittest.TestCase):
    def test_start_mode(self):
        self.assertEqual((H.CHfm().mode, H.CHfm().src), ("phone", "build"))
        self.assertEqual(H.CHfm(default_mode="pc").mode, "pc")
        m = H.CHfm(nvs_mode="phone", default_mode="pc")
        self.assertEqual((m.mode, m.src), ("phone", "nvs"))       # the last explicit choice beats the build

    def test_migration_from_phone_bringup(self):
        m = H.CHfm(mru0=PHONE)
        self.assertEqual(m.slot("phone"), PHONE)
        self.assertTrue(m.dirty and m.s.migrated)
        # not once a mode was stored: "mode forget phone" must not come back at the next start
        m2 = H.CHfm(nvs_mode="pc", mru0=PHONE)
        self.assertIsNone(m2.slot("phone"))
        self.assertFalse(m2.dirty)

    def test_slc_table(self):
        m = H.CHfm(nvs_mode="phone", phone=PHONE, pc=PC)
        self.assertEqual(m.on_slc(PHONE), "keep")
        self.assertEqual(m.on_slc(PC), "drop_other")
        self.assertEqual(m.on_slc(OTHER), "drop_unknown")
        m.command("pc")
        self.assertEqual(m.on_slc(PC), "keep")
        self.assertEqual(m.on_slc(PHONE), "drop_other")
        # empty slot: the first AG that is not the other mode's takes it
        m = H.CHfm(nvs_mode="pc", phone=PHONE)
        self.assertEqual(m.on_slc(PHONE), "drop_other")
        self.assertEqual(m.on_slc(PC), "adopt")
        self.assertEqual(m.slot("pc"), PC)
        self.assertEqual(m.on_slc(OTHER), "drop_unknown")

    def test_new_pairing_takes_the_slot(self):
        m = H.CHfm(nvs_mode="pc", phone=PHONE, pc=PC)
        m.on_paired(NEW_PC)
        self.assertEqual(m.on_slc(NEW_PC), "adopt")
        self.assertEqual(m.slot("pc"), NEW_PC)
        self.assertEqual(m.on_slc(PC), "drop_unknown")             # the old PC is no longer this mode's
        # the phone paired anew while in the PC mode moves to the PC slot: one AG, one slot
        m.on_paired(PHONE)
        self.assertEqual(m.on_slc(PHONE), "adopt")
        self.assertEqual((m.slot("pc"), m.slot("phone")), (PHONE, None))

    def test_whom_to_call(self):
        m = H.CHfm(nvs_mode="phone", phone=PHONE, pc=PC)
        self.assertEqual(m.next_target([PC, PHONE, OTHER]), PHONE)
        m = H.CHfm(nvs_mode="pc", phone=PHONE)
        got = [m.next_target([PHONE, PC, OTHER]) for _ in range(4)]
        self.assertEqual(got, [PC, OTHER, PC, OTHER])               # round robin, never the phone
        self.assertIsNone(H.CHfm(nvs_mode="pc", phone=PHONE).next_target([PHONE]))
        self.assertIsNone(H.CHfm().next_target([]))

    def test_command(self):
        m = H.CHfm(nvs_mode="phone", phone=PHONE, pc=PC)
        act, text = m.command("", slc_bda=PHONE)
        self.assertEqual((act, text), (0, "mode=phone src=nvs phone=..:44:01 pc=..:44:02 link=phone ..:44:01"))
        self.assertEqual(m.command("phone"), (0, "mode phone unchanged"))
        act, text = m.command("pc", sco_on=True, slc_bda=PHONE)
        self.assertEqual((act, m.mode), (0, "phone"))
        self.assertEqual(text, "mode refused: SCO active, 'mode pc 1' forces")
        act, text = m.command("pc 1", sco_on=True, slc_bda=PHONE)
        self.assertEqual(act, H.HFM_DO_SAVE | H.HFM_DO_DROP | H.HFM_DO_KICK)
        self.assertEqual(text, "mode pc saved, was phone, dropping the link to phone")
        act, text = m.command("phone")                              # no link: save and call at once
        self.assertEqual(act, H.HFM_DO_SAVE | H.HFM_DO_KICK)
        for bad in ("tv", "pc 2", "pc 1 x", "forget", "forget tv"):
            self.assertEqual(m.command(bad)[0], 0, bad)
            self.assertTrue(m.command(bad)[1].startswith("mode refused: bad request"), bad)
        act, text = m.command("forget pc")
        self.assertEqual((act, text, m.slot("pc")), (H.HFM_DO_SAVE, "mode forget pc: slot was ..:44:02", None))
        m.forget_all()
        self.assertEqual((m.slot("phone"), m.slot("pc"), m.mode), (None, None, "phone"))


# --- scenarios on the model ------------------------------------------------------------------------------------------------

def bench(mode=None, phone_slot=True, pc_slot=False, bonds=("phone",), peers=("phone",), default="phone",
          pc_codec="CVSD", aggressive=False, lyrat_cls=S.Lyrat):
    """A World with the phone and the PC as AGs. nvs as a board of that state would hold it."""
    addr = {"phone": PHONE, "pc": PC, "other": OTHER}
    nvs = {"peers": [addr[p] for p in peers]}
    if mode:
        nvs["mode"] = mode
        if phone_slot:
            nvs["peer_ph"] = PHONE
        if pc_slot:
            nvs["peer_pc"] = PC
    w = S.World(nvs=nvs, bonds=[addr[b] for b in bonds], default_mode=default, lyrat_cls=lyrat_cls)
    w.phone = w.add(S.Phone(w, PHONE, aggressive=aggressive))
    w.pc = w.add(S.Windows(w, PC, codec=pc_codec))
    w.pc.paired = "pc" in bonds
    w.start()
    return w


def up_with(w, kind):
    return lambda: w.link() == (kind, "slc") and w.lyrat.slc_up


class PhoneMode(unittest.TestCase):
    def test_first_start_after_phone_bringup(self):
        w = bench()                                    # NVS of phone-bringup: the reconnect list, no mode
        took = w.clock.run_until_cond(up_with(w, "phone"), 20000)
        self.assertIsNotNone(took)
        self.assertLessEqual(took, w.lyrat.first_ms + S.PAGE_MS + S.SLC_MS)
        self.assertEqual((w.nvs["mode"], w.nvs["peer_ph"]), ("phone", PHONE))
        self.assertIn(" migrated", w.lyrat.log[1])
        s = summary(w)
        self.assertEqual((s["state"], s["mode"], s["link"]["role"]), ("slc", "phone", "phone"))

    def test_call_audio(self):
        for codec in ("mSBC", "CVSD"):
            w = bench(mode="phone")
            w.phone.codec = codec
            w.clock.run_until_cond(up_with(w, "phone"), 20000)
            w.phone.call()
            w.clock.run_for(1000)
            self.assertTrue(w.lyrat.sco_active)
            s = summary(w)
            self.assertEqual((s["state"], s["codec"]), ("sco", codec))
            w.phone.hang_up()
            w.clock.run_for(500)
            self.assertFalse(w.lyrat.sco_active)
            self.assertEqual(summary(w)["state"], "slc")

    def test_phone_comes_by_itself(self):
        w = bench(mode="phone")
        w.phone.bt_on = False
        w.clock.run_for(30000)
        self.assertNotEqual(w.link(), ("phone", "slc"))
        self.assertEqual(summary(w)["hint"].split(" and it")[0], "LyraT calls the phone ..:44:01")
        w.phone.come_back()
        self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "phone"), 10000))

    def test_same_as_phone_bringup(self):
        """Phone only: the lines of before goal 102 come at the same times with the same text, also over a link loss,
        a call and a reboot; the new lines are only the "mode ..." ones."""
        def run(cls):
            w = bench(lyrat_cls=cls, mode=None)
            w.clock.run_for(15000)
            w.phone.call()
            w.clock.run_for(2000)
            w.phone.hang_up()
            w.clock.run_for(3000)
            w.phone.leave()
            w.clock.run_for(20000)
            w.phone.come_back()
            w.clock.run_for(15000)
            w.reboot()
            w.clock.run_for(15000)
            return [ln for ln in w.lyrat.log if not re.search(r": mode ", ln)], w
        new, wn = run(S.Lyrat)
        old, wo = run(S.OldLyrat)
        self.assertEqual(new, old)
        self.assertGreater(len(old), 30)
        self.assertEqual(wn.phone.links, wo.phone.links)
        self.assertEqual(wn.stack.ignored, wo.stack.ignored)

    def test_old_second_bond_is_not_paged(self):
        """A board of phone-bringup with an old PC in its list: phone-bringup pages both in turn; the phone mode only
        the phone (DECISIONS D2) - the phone comes back in one period instead of two."""
        tries = {}
        for cls in (S.OldLyrat, S.Lyrat):
            w = bench(lyrat_cls=cls, bonds=("phone", "pc"), peers=("phone", "pc"))
            w.phone.bt_on = False
            w.clock.run_for(40000)
            tries[cls.__name__] = [ln for ln in w.lyrat.log if "HFP connect try" in ln]
        self.assertTrue(any(ln.endswith("44:02") for ln in tries["OldLyrat"]))
        self.assertFalse(any(ln.endswith("44:02") for ln in tries["Lyrat"]))


class PcMode(unittest.TestCase):
    def test_switch_while_idle_then_zoom(self):
        for codec in ("CVSD", "mSBC"):
            w = bench(mode="phone", pc_slot=True, bonds=("phone", "pc"), pc_codec=codec)
            w.clock.run_until_cond(up_with(w, "phone"), 20000)
            reply = w.lyrat.command("mode pc")
            self.assertEqual(reply, "mode pc saved, was phone, dropping the link to phone")
            took = w.clock.run_until_cond(up_with(w, "pc"), 10000)
            # the drop wakes the reconnect task: no wait for its period
            self.assertLessEqual(took, S.DISC_MS + S.PAGE_MS + S.SLC_MS)
            self.assertEqual(w.nvs["mode"], "pc")
            w.pc.open_audio()                       # Zoom takes the Hands-Free endpoint
            w.clock.run_for(500)
            s = summary(w)
            self.assertEqual((s["state"], s["codec"], s["link"]["role"]), ("sco", codec, "pc"))
            w.pc.close_audio()
            w.clock.run_for(500)
            self.assertEqual(summary(w)["hint"], hfp_states.HINTS["slc_pc_no_sco"])

    def test_switch_refused_during_sco_and_forced(self):
        w = bench(mode="phone", pc_slot=True, bonds=("phone", "pc"))
        w.clock.run_until_cond(up_with(w, "phone"), 20000)
        w.phone.call()
        w.clock.run_for(1000)
        self.assertEqual(w.lyrat.command("mode pc"), "mode refused: SCO active, 'mode pc 1' forces")
        w.clock.run_for(5000)
        self.assertEqual((w.link(), w.nvs["mode"], w.lyrat.sco_active), (("phone", "slc"), "phone", True))
        w.lyrat.command("mode pc 1")
        self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "pc"), 5000))
        s = summary(w)
        self.assertEqual((s["counts"]["mode_refused"], s["counts"]["mode_switch"]), (1, 1))

    def test_app_opens_and_closes_often(self):
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc"))
        w.clock.run_until_cond(up_with(w, "pc"), 20000)
        for i in range(40):
            w.pc.open_audio("mSBC" if i % 2 else "CVSD")
            w.clock.run_for(200 + 37 * (i % 5))
            w.pc.close_audio()
            w.clock.run_for(50 + 11 * (i % 3))
        s = summary(w)
        self.assertEqual((s["counts"]["sco_open"], s["counts"]["sco_close"]), (40, 40))
        self.assertEqual((s["state"], s["counts"]["slc_down"], s["counts"]["drops"]), ("slc", 0, 0))
        self.assertFalse(w.lyrat.sco_active)

    def test_pc_away_and_back_with_a_pushy_phone(self):
        """The PC sleeps; the phone keeps connecting all the time: each of its links is dropped, the board stays the
        PC's and gets it back within one period once it wakes."""
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc"), aggressive=True)
        w.phone.come_back(500)
        w.clock.run_until_cond(up_with(w, "pc"), 20000)
        w.pc.leave()
        w.clock.run_for(60000)
        self.assertGreater(w.phone.dropped, 3)
        self.assertNotEqual(w.link(), ("phone", "slc"))
        self.assertEqual(summary(w)["hint"], hfp_states.HINTS["drop_other"].format(role="phone", mode="pc"))
        w.pc.come_back()
        took = w.clock.run_until_cond(up_with(w, "pc"), 30000)
        self.assertIsNotNone(took, "the PC never came back: the phone's links took the slot every time")
        self.assertLessEqual(took, w.lyrat.period_ms + S.PAGE_TIMEOUT_MS + S.PAGE_MS + S.SLC_MS)
        self.assertEqual(w.nvs["mode"], "pc")

    def test_reboot_keeps_the_mode(self):
        w = bench(mode="phone", pc_slot=True, bonds=("phone", "pc"))
        w.clock.run_until_cond(up_with(w, "phone"), 20000)
        w.lyrat.command("mode pc")
        w.clock.run_until_cond(up_with(w, "pc"), 10000)
        w.reboot()
        took = w.clock.run_until_cond(up_with(w, "pc"), 20000)
        self.assertIsNotNone(took)
        self.assertIn("mode boot: mode=pc src=nvs", w.lyrat.log[-1 - next(
            i for i, ln in enumerate(reversed(w.lyrat.log)) if "mode boot" in ln)])
        w.lyrat.command("mode phone")
        self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "phone"), 10000))

    def test_first_pairing_of_windows(self):
        for after_pair in (True, False):
            w = bench(mode="phone")                 # the bench today: the phone only
            w.pc.paired = False
            w.pc.connect_after_pair = after_pair
            w.clock.run_until_cond(up_with(w, "phone"), 20000)
            w.lyrat.command("mode pc")
            w.clock.run_for(3000)
            self.assertIsNone(w.link())             # nobody to call yet: the PC slot is empty, the phone is excluded
            self.assertEqual(summary(w)["hint"], hfp_states.HINTS["no_target"].format(mode="pc"))
            w.pc.pair()
            took = w.clock.run_until_cond(up_with(w, "pc"), 20000)
            self.assertIsNotNone(took, "after_pair=%s" % after_pair)
            self.assertEqual(w.nvs["peer_pc"], PC)
            w.lyrat.command("mode phone")
            self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "phone"), 10000))
            w.lyrat.command("mode pc")
            self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "pc"), 10000))

    def test_unknown_ag(self):
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc", "other"))
        w.other = w.add(S.Phone(w, OTHER, name="Other"))
        w.pc.bt_on = False
        w.other.come_back(5000)                     # it tries a few times; some land while the PC is paged
        w.clock.run_for(20000)
        self.assertGreaterEqual(w.other.dropped, 1)
        self.assertEqual(summary(w)["last_drop"]["why"], "drop_unknown")
        self.assertNotEqual(w.link(), ("phone", "slc"))
        w.lyrat.command("mode forget pc")
        w.other.come_back(100)
        # the model class of OTHER is Phone, hence its kind; it is the PC mode's AG now
        self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "phone"), 20000))
        self.assertEqual(w.nvs["peer_pc"], OTHER)

    def test_build_pc_on_a_clean_board(self):
        w = bench(mode=None, bonds=(), peers=(), default="pc")
        w.clock.run_for(100)
        self.assertIn("mode boot: mode=pc src=build phone=none pc=none", w.lyrat.log[1])
        self.assertNotIn("mode", w.nvs.get("peers", []))
        w.pc.pair()
        self.assertIsNotNone(w.clock.run_until_cond(up_with(w, "pc"), 10000))
        self.assertEqual((w.nvs["mode"], w.nvs["peer_pc"]), ("pc", PC))

    def test_unpair_forgets_both(self):
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc"))
        w.clock.run_until_cond(up_with(w, "pc"), 20000)
        w.lyrat.command("unpair")
        self.assertNotIn("peer_ph", w.nvs)
        self.assertNotIn("peer_pc", w.nvs)

    def test_windows_hfp_service_off(self):
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc"))
        w.pc.hfp_service = False
        w.clock.run_for(30000)
        self.assertIsNone(w.link())
        s = summary(w)
        self.assertEqual(s["hint"], hfp_states.HINTS["acl_no_slc"])


# --- the log reader and the source ---------------------------------------------------------------------------------------

class Source(unittest.TestCase):
    def test_reader_patterns_are_lines_of_main_c(self):
        for kind, (_, literal) in hfp_states.PATTERNS.items():
            self.assertIn(literal, SRC, kind)

    def test_model_formats_are_lines_of_main_c(self):
        for key, f in S.FMT.items():
            self.assertIn(f, SRC, key)

    def test_glue_calls_the_rules(self):
        for call in ("hfm_init(&s_hfm", "hfm_on_slc(&s_hfm", "hfm_next_target(&s_hfm", "hfm_command(&s_hfm",
                     "hfm_on_paired(&s_hfm", "hfm_forget_all(&s_hfm", "hfm_slc_keep(param->conn_stat.remote_bda)",
                     "xTaskCreatePinnedToCore(hf_reconnect_task, \"hf_recon\", 3072, NULL, 6, &s_recon_task, 0)"):
            self.assertIn(call, SRC, call)
        # a dropped link must never print SLC CONNECTED: the keep test comes first in the SLC branch
        branch = SRC[SRC.index("ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {"):]
        self.assertLess(branch.index("hfm_slc_keep"), branch.index("*** SLC CONNECTED ***"))
        # the mode and its slots before the board can be seen
        init = SRC[SRC.index("static esp_err_t bt_init"):]
        self.assertLess(init.index("hfm_load();"), init.index("esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE"))
        self.assertLess(init.index("s_hfm_mutex = xSemaphoreCreateMutex()"), init.index("esp_bt_gap_register_callback"))

    def test_audio_path_same_as_phone_bringup(self):
        """The sound of both modes is the code of phone-bringup: these functions are byte for byte the same."""
        ref = subprocess.run(["git", "-C", str(MAIN), "show", "origin/phone-bringup:lyrat_resend/main/main.c"],
                             capture_output=True, text=True, encoding="utf-8")
        if ref.returncode != 0:
            self.fail("no origin/phone-bringup in this repo: %s" % ref.stderr.strip())
        old = ref.stdout

        # Goal 111 put the mic mute into mic_rx_task on purpose (after the tone, before the ring to Bluetooth; its own
        # tests are test_mic_mute.Glue). That block is taken out of both sides, the rest stays byte for byte.
        mute_block = ("        // Goal 111: the mute, after the tone and everything else; the frames keep going, only the samples "
                      "ramp to 0\n        mm_apply(&s_mm, mono_buf, n);\n\n")

        def body(src, name):
            src = src.replace("\r\n", "\n").replace(mute_block, "")
            i = src.index(name)
            j = src.index("\n}\n", i)
            return src[i:j]
        for name in ("static void sco_frame_in(", "static void sco_frames_out(", "static void msbc_frame_in(",
                     "static void cvsd_block(", "static void cvsd_packet_in(", "static bool msbc_frame_out(",
                     "static bool cvsd_frame_out(", "static void i2s_tx_task(", "static void mic_rx_task(",
                     "static esp_err_t audio_init(", "static void scan_mode_apply("):
            self.assertEqual(body(SRC, name), body(old, name), name)
        # the SCO branch of the HFP callback too
        self.assertEqual(body(SRC, "case ESP_HF_CLIENT_AUDIO_STATE_EVT:"), body(old, "case ESP_HF_CLIENT_AUDIO_STATE_EVT:"))

    def test_reader_on_udplog_lines(self):
        """The same log as udplog.py writes it (PC time, board, board seconds); the S3's lines are left out."""
        w = bench(mode="pc", pc_slot=True, bonds=("phone", "pc"))
        w.clock.run_until_cond(up_with(w, "pc"), 20000)
        net = []
        for ln in w.lyrat.log:
            m = re.match(r"[IW] \((\d+)\) \w+: (.*)", ln)
            net.append("10:00:00.000 lyrat %9.3f %s" % (int(m.group(1)) / 1000.0, m.group(2)))
            net.append("10:00:00.000 s3    %9.3f SLC DOWN after 1 s, last_acl_reason=0x8" % 1.0)
        s = hfp_states.summarize(hfp_states.parse(net))
        self.assertEqual((s["state"], s["mode"], s["link"]["role"], s["counts"]["slc_down"]), ("slc", "pc", "pc", 0))
        self.assertIsNotNone(s["timeline"][-1]["t"])

    def test_pc_link_verdicts(self):
        import pc_link
        self.assertEqual(pc_link.verdict(pc_link.devices(""))[0], "not_paired")
        devs = [{"FriendlyName": "LyraT_Phone", "Class": "Bluetooth", "Status": "OK", "Present": True},
                {"FriendlyName": "LyraT_Phone Hands-Free AG", "Class": "System", "Status": "OK", "Present": True},
                {"FriendlyName": "Headset Microphone (LyraT_Phone Hands-Free)", "Class": "AudioEndpoint",
                 "Status": "Unknown", "Present": False},
                {"FriendlyName": "Headphones (LyraT_Phone Hands-Free)", "Class": "AudioEndpoint", "Status": "Unknown",
                 "Present": False}]
        self.assertEqual(pc_link.verdict(devs)[0], "paired")
        for d in devs[2:]:
            d.update(Status="OK", Present=True)
        v, rows = pc_link.verdict(pc_link.devices(__import__("json").dumps(devs)))
        self.assertEqual((v, [r["kind"] for r in rows]), ("connected", ["bt", "hfp", "mic", "speaker"]))
        self.assertEqual(pc_link.verdict(pc_link.devices(__import__("json").dumps(devs[0])))[0], "paired")

    def test_reader_cli(self):
        w = bench(mode="phone")
        w.clock.run_until_cond(up_with(w, "phone"), 20000)
        r = subprocess.run([sys.executable, str(HERE.parent / "bench" / "hfp_states.py"), "-"],
                           input="\n".join(w.lyrat.log), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("state=slc mode=phone link=phone", r.stdout)
        r = subprocess.run([sys.executable, str(HERE.parent / "bench" / "hfp_states.py"), "-"],
                           input="nothing here\n", capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
