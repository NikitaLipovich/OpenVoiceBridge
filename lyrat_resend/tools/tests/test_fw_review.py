"""Findings of the code review of goal 094 that the PC can check: pure C through otahost.dll, the rest read from the
firmware source the way s3_sim.py reads it. Each class names its finding; docs/TESTS.md lists them. Made by goal 094.

What only the boards can show (radio, flash, the bootloader) is in the REPORT of goal 094 as "проверит 091".
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
REPO = TOOLS.parent.parent
sys.path.insert(0, str(TOOLS / "ota_test"))
import otahost as H  # noqa: E402

S3_MAIN = REPO / "s3_eth_bridge" / "main" / "main.c"
S3_OTA = REPO / "s3_eth_bridge" / "main" / "s3_ota.c"
LYRAT = REPO / "lyrat_resend" / "main"


def read(path):
    return path.read_text(encoding="utf-8")


def block(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def strip_comments(text):
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)


class SoftLimiter(unittest.TestCase):
    """#004: call_soft_limit() (LyraT, console "lim") multiplied excess by headroom in 32 bits. With the call gain 5..8
    and a loud phone the product overflowed and a peak came out with the opposite sign: a click in the ear."""

    def test_full_scale_times_every_gain(self):
        for gain in range(1, 9):
            for sample in (32767, -32768, 20000, -20000, 1000):
                x = sample * gain
                y = H.soft_limit(x)
                self.assertEqual(y > 0, x > 0, (gain, sample, y))          # never the opposite sign
                self.assertLessEqual(abs(y), 32700, (gain, sample, y))

    def test_linear_zone_untouched(self):
        for x in (0, 1, -1, 1000, -16384, 16384):
            self.assertEqual(H.soft_limit(x), x)

    def test_monotonic_and_symmetric(self):
        last = None
        for x in range(0, 8 * 32768 + 1, 977):
            y = H.soft_limit(x)
            self.assertEqual(H.soft_limit(-x), -y)
            if last is not None:
                self.assertGreaterEqual(y, last, x)
            last = y
        self.assertGreater(last, 30000)

    def test_main_uses_the_shared_limiter(self):
        src = strip_comments(read(LYRAT / "main.c"))
        self.assertIn('#include "call_limit.h"', src)
        self.assertIn("call_soft_limit(s)", block(src, "static void i2s_tx_task", "\n}\n"))
        self.assertNotIn("excess * headroom", src)


class S3WifiRetry(unittest.TestCase):
    """#003: every disconnect called esp_wifi_connect() at once. With the router off that is a scan of every channel at
    full power without a break, next to the LyraT's Bluetooth (R13). Now a few at once, then one per WIFI_RETRY_MS."""

    def setUp(self):
        self.src = strip_comments(read(S3_MAIN))
        self.handler = block(self.src, "static void on_wifi_event", "\n}\n")
        self.d = {m.group(1): int(m.group(2)) for m in re.finditer(r"#define\s+(WIFI_RETRY_\w+)\s+(\d+)", read(S3_MAIN))}

    def test_disconnect_goes_through_the_pause(self):
        lost = block(self.handler, "WIFI_EVENT_STA_DISCONNECTED", "IP_EVENT_STA_GOT_IP")
        self.assertIn("wifi_retry();", lost)
        self.assertNotIn("esp_wifi_connect", lost)

    def test_an_ip_resets_the_count(self):
        got_ip = self.handler[self.handler.index("IP_EVENT_STA_GOT_IP"):]
        self.assertRegex(got_ip, r"s_wifi_fails\s*=\s*0")

    def test_pause_fits_the_self_test(self):
        # A new image must still find its network in the 90 s of its self-test: many attempts fit into that time.
        selftest_ms = int(re.search(r"#define SELFTEST_MS\s+(\d+)", read(S3_OTA)).group(1))
        self.assertTrue(1 <= self.d["WIFI_RETRY_AT_ONCE"] <= 5, self.d)
        self.assertTrue(2000 <= self.d["WIFI_RETRY_MS"] <= selftest_ms // 6, self.d)

    def test_timer_retries_when_connect_did_not_start(self):
        now = block(self.src, "static void wifi_retry_now", "\n}\n")
        self.assertIn("esp_wifi_connect()", now)
        self.assertIn("esp_timer_start_once(s_wifi_retry", now)
        init = block(self.src, "static esp_err_t net_init(void)\n{\n    ESP_RETURN_ON_ERROR(esp_netif_init()", "return ESP_OK;")
        self.assertIn("esp_timer_create(&retry, &s_wifi_retry)", init)


class S3RestartDelay(unittest.TestCase):
    """#005: RESTART_DELAY_MS ("UX") was defined only inside #ifndef CTL_TX_MODE_BOOT, so a build with
    -DCTL_TX_MODE_BOOT=... did not compile. Every #define of the file must be outside that block now."""

    def test_defined_whatever_the_tx_mode(self):
        src = read(S3_MAIN)
        guard = block(src, "#ifndef CTL_TX_MODE_BOOT", "#endif")
        self.assertEqual(re.findall(r"#define\s+(\w+)", guard), ["CTL_TX_MODE_BOOT"])
        self.assertRegex(src, r"(?m)^#define RESTART_DELAY_MS\s+\d+")


class SelfTestOnlyOnce(unittest.TestCase):
    """TASK 094 item 1, router off: only an image in PENDING_VERIFY runs the self-test, which the bootloader gives to
    the first start after an update only (the next start turns it into ABORTED). The image it falls back to is VALID and
    has no self-test, so a working image never rolls back for want of a network and there is no loop. The S3 self-test
    does require an IP: an image that cannot reach the network must not stay on a bench without USB."""

    def check_gate(self, src):
        gate = re.search(r"if \(([^\n]*ESP_OTA_IMG_PENDING_VERIFY[^\n]*)\)\s*\{\s*xTaskCreatePinnedToCore\(selftest_task", src)
        self.assertIsNotNone(gate)
        self.assertIn("esp_ota_get_state_partition(run, &st) == ESP_OK", gate.group(1))
        self.assertEqual(len(re.findall(r"xTaskCreatePinnedToCore\(selftest_task", src)), 1)

    def test_s3(self):
        src = strip_comments(read(S3_OTA))
        self.check_gate(src)
        ok = re.search(r"bool ok = ([^;]+);", block(src, "static void selftest_task", "\n}\n")).group(1)
        self.assertIn("h.got_ip", ok)

    def test_lyrat(self):
        self.check_gate(strip_comments(read(LYRAT / "lyrat_ota.c")))

    def test_wifi_keeps_trying_forever(self):
        # No give-up path: a valid image without a network keeps asking, it never restarts for it.
        src = strip_comments(read(S3_MAIN))
        handler = block(src, "static void on_wifi_event", "\n}\n")
        self.assertNotIn("esp_restart", handler)
        self.assertNotIn("esp_restart", block(src, "static void wifi_retry_now", "\n}\n"))


class FailtestNeverPasses(unittest.TestCase):
    """Goal 096, incident 002: with FAILTEST the self-test must fail. Its conditions (Bluetooth and a frame of the S3, or
    the S3's network) can hold within a second, before the 5 s of the planned failure, so "ok" is forced false from the
    first round on, on both boards; the old code let the image confirm itself on the hardware."""

    def check(self, src):
        task = block(strip_comments(src), "static void selftest_task", "\n}\n")
        fail = re.search(r"if \(fail_on_purpose\) \{\s*ok = false;", task)
        self.assertIsNotNone(fail, "ok must be false at once with fail_on_purpose")
        self.assertLess(fail.start(), task.index("if (ok)"))

    def test_lyrat(self):
        self.check(read(LYRAT / "lyrat_ota.c"))

    def test_s3(self):
        self.check(read(S3_OTA))


class NoiseModeAtStart(unittest.TestCase):
    """#006: the default noise mode (web2) was started without checking that ns_pro_create() gave it a handle;
    ns_process(NULL) then ran in the mic task. The start and voice_ns_request() share one rule now."""

    def test_start_checks_the_handle(self):
        src = strip_comments(read(LYRAT / "voice_ns.c"))
        init = block(src, "void voice_ns_init(void)", "\n}\n")
        self.assertLess(init.index("mode_available(s_cur)"), init.index("mode_start(s_cur)"))
        self.assertIn("mode_available(mode)", block(src, "void voice_ns_request(int mode)", "\n}\n"))
        rule = block(src, "static bool mode_available(int mode)", "\n}\n")
        self.assertIn("s_web[mode] != NULL", rule)


IDF_DEFAULT_HZ = 100      # ESP-IDF's CONFIG_FREERTOS_HZ when sdkconfig.defaults does not set it


def project_hz(project_dir):
    m = re.search(r"^CONFIG_FREERTOS_HZ=(\d+)", read(project_dir / "sdkconfig.defaults"), flags=re.M)
    return int(m.group(1)) if m else IDF_DEFAULT_HZ


def defines(src):
    return {m.group(1): m.group(2) for m in re.finditer(r"^#define\s+(\w+)\s+\(?\s*(\d+)\s*\)?", src, flags=re.M)}


def zero_tick_delays(project_dir):
    """vTaskDelay(pdMS_TO_TICKS(X)) whose X is below one tick: that is vTaskDelay(0), a task that never lets the lower
    ones of its core run. X is a number or a #define of the same file; other expressions are not judged."""
    hz = project_hz(project_dir)
    found = []
    for path in sorted((project_dir / "main").glob("*.c")):
        src = strip_comments(read(path))
        consts = defines(src)
        for m in re.finditer(r"vTaskDelay\(\s*pdMS_TO_TICKS\(\s*(\w+)\s*\)\s*\)", src):
            arg = m.group(1)
            ms = int(arg) if arg.isdigit() else int(consts[arg]) if arg in consts else None
            if ms is not None and ms * hz // 1000 == 0:
                found.append("%s:%d vTaskDelay(pdMS_TO_TICKS(%s)) = 0 ticks at %d Hz"
                             % (path.name, src.count("\n", 0, m.start()) + 1, arg, hz))
    return found


class NoZeroTickDelay(unittest.TestCase):
    """Goal 095 #003: relay_task of the S3 slept vTaskDelay(pdMS_TO_TICKS(5)); the S3 runs at 100 Hz, so that was 0
    ticks and the task at priority 4 starved CPU0: no UDP log, no stat line, no self-test, task watchdog every 5 s."""

    def test_s3(self):
        self.assertEqual(project_hz(REPO / "s3_eth_bridge"), 100)
        self.assertEqual(zero_tick_delays(REPO / "s3_eth_bridge"), [])

    def test_lyrat(self):
        self.assertEqual(zero_tick_delays(REPO / "lyrat_resend"), [])

    def test_relay_poll_has_a_floor(self):
        src = strip_comments(read(S3_OTA))
        self.assertIn("vTaskDelay(RELAY_POLL_TICKS)", block(src, "static void relay_task(void *arg)", "\n}\n"))
        self.assertRegex(src, r"#define RELAY_POLL_TICKS .*: 1\)")

    def test_the_check_finds_the_old_bug(self):
        old = "#define POLL_MS 5\nvoid t(void) { vTaskDelay(pdMS_TO_TICKS(5)); vTaskDelay(pdMS_TO_TICKS(POLL_MS)); " \
              "vTaskDelay(pdMS_TO_TICKS(10)); }\n"
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "main").mkdir()
            (p / "main" / "x.c").write_text(old, encoding="utf-8")
            (p / "sdkconfig.defaults").write_text("", encoding="utf-8")
            self.assertEqual(len(zero_tick_delays(p)), 2)
            (p / "sdkconfig.defaults").write_text("CONFIG_FREERTOS_HZ=1000\n", encoding="utf-8")
            self.assertEqual(zero_tick_delays(p), [])


if __name__ == "__main__":
    unittest.main()
