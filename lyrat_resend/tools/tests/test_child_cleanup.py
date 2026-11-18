"""Processes started by a test run end with it, whatever the outcome (goal 107, after goal 106 #001: a failed run left
s3_sim.py holding ota_test/build/otahost.dll and the next build failed with LNK1104).

The real run_tests.py is run on fake checks (its hidden --check-file). Each fake starts a child that starts a grandchild;
both load a copy of a DLL, ignore Ctrl+C and Ctrl+Break and sleep - the worst case of a stuck simulator. After a check
that passes, fails, crashes or runs past its timeout, after Ctrl+Break on the runner and after the runner is killed
from outside, none of them runs and the DLL can be opened for writing again. A process started beside the run is not
touched, nor a bench service listening on its port. A test process tied by procjob.tie_this_process() (simproc does it)
takes its children with it when it is killed. Last: ota_test/build/otahost.dll is free, every check before this one
having run.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
RUN_TESTS = TOOLS / "run_tests.py"
OTAHOST = TOOLS / "ota_test" / "build" / "otahost.dll"
sys.path.insert(0, str(HERE))
import procjob  # noqa: E402

# child of a fake check: loads the DLL, starts the grandchild (same code, depth 1), writes the pids, sleeps
SLEEPER = r'''
import ctypes, os, signal, subprocess, sys, time
for s in ("SIGINT", "SIGBREAK"):
    if hasattr(signal, s):
        signal.signal(getattr(signal, s), signal.SIG_IGN)
dll, pidfile, depth = sys.argv[1], sys.argv[2], int(sys.argv[3])
ctypes.CDLL(dll)
if depth < 1:
    subprocess.Popen([sys.executable, __file__, dll, pidfile, str(depth + 1)])
with open(pidfile, "a") as f:
    f.write("%d\n" % os.getpid())
time.sleep(600)
'''

# a fake check: starts the sleeper tree, waits for both pids, then ends as told
FAKE = r'''
import os, signal, subprocess, sys, time
for s in ("SIGINT", "SIGBREAK"):
    if hasattr(signal, s):
        signal.signal(getattr(signal, s), signal.SIG_IGN)
sleeper, dll, pidfile, mode = sys.argv[1:5]
subprocess.Popen([sys.executable, sleeper, dll, pidfile, "0"])
t = time.time()
while time.time() - t < 30:
    if os.path.exists(pidfile) and len(open(pidfile).read().split()) >= 2:
        break
    time.sleep(0.05)
print("tree up", flush=True)
if mode == "pass":
    sys.exit(0)
if mode == "fail":
    sys.exit(1)
if mode == "crash":
    os._exit(3)          # no cleanup of any kind
time.sleep(600)          # "hang": the timeout, Ctrl+Break or a kill ends it
'''

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008

# Ctrl+C as a person gives it: to every process of run_tests' console. The sender joins that console and ignores the
# event itself; it is a process of its own so the test's console stays as it is.
CTRL_C_SENDER = r'''
import ctypes, sys
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit("AttachConsole: %d" % ctypes.get_last_error())
k.SetConsoleCtrlHandler(None, True)
if not k.GenerateConsoleCtrlEvent(0, 0):
    sys.exit("GenerateConsoleCtrlEvent: %d" % ctypes.get_last_error())
'''


def hidden_console():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return {"creationflags": CREATE_NEW_CONSOLE, "startupinfo": si}


def dll_free(path, wait=5.0):
    """Whether the file can be opened for writing, as the linker does; the image goes a few ms after the process."""
    t = time.monotonic()
    while True:
        try:
            open(path, "r+b").close()
            return True
        except PermissionError:
            if time.monotonic() - t > wait:
                return False
            time.sleep(0.05)


def listening(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


@unittest.skipUnless(procjob.WINDOWS, "Windows job objects")
class ChildCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="t107_"))
        cls.sleeper = cls.tmp / "sleeper.py"
        cls.sleeper.write_text(SLEEPER, encoding="utf-8")
        cls.fake = cls.tmp / "fake.py"
        cls.fake.write_text(FAKE, encoding="utf-8")
        # bystander: started by this test outside any run; must survive everything below
        cls.bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        cls.benchd_port = 5080      # the live service is on the default port, whatever offset the tests use
        cls.benchd_was_up = listening(cls.benchd_port)

    @classmethod
    def tearDownClass(cls):
        cls.bystander.kill()
        cls.bystander.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.assertIsNone(self.bystander.poll(), "a process outside the run was killed")
        self.assertEqual(listening(self.benchd_port), self.benchd_was_up, "the bench service on 5080 changed")

    def case(self, name):
        d = self.tmp / name
        d.mkdir()
        dll = d / "probe.dll"
        shutil.copy(Path(sys.base_prefix) / "python3.dll", dll)
        return d, dll

    def check(self, name, d, dll, mode, timeout=60):
        return {"name": name, "cmd": [sys.executable, str(self.fake), str(self.sleeper), str(dll),
                                      str(d / (name + ".pids")), mode], "cwd": str(d), "timeout": timeout}

    def pids(self, d, name, n=2, wait=30):
        f = d / (name + ".pids")
        t = time.time()
        while time.time() - t < wait:
            if f.exists() and len(f.read_text().split()) >= n:
                return [int(x) for x in f.read_text().split()]
            time.sleep(0.05)
        self.fail("the sleeper tree of %s did not come up" % name)

    def run_tests(self, d, checks, **kw):
        cf = d / "checks.json"
        cf.write_text(json.dumps(checks), encoding="utf-8")
        return subprocess.Popen([sys.executable, str(RUN_TESTS), "--check-file", str(cf), "--out", str(d / "run")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", **kw)

    def gone(self, pids, wait=10):
        t = time.time()
        while time.time() - t < wait and any(procjob.pid_alive(p) for p in pids):
            time.sleep(0.1)
        return [p for p in pids if procjob.pid_alive(p)]

    def summary(self, d):
        return json.loads((d / "run" / "summary.json").read_text(encoding="utf-8"))

    def test_a_pass_fail_crash_timeout(self):
        d, dll = self.case("outcomes")
        modes = [("pass", "pass", 60), ("fail", "fail", 60), ("crash", "crash", 60), ("timeout", "hang", 4)]
        p = self.run_tests(d, [self.check(n, d, dll, m, t) for n, m, t in modes])
        out, _ = p.communicate(timeout=180)
        self.assertEqual(p.returncode, 1, out)
        res = {c["name"]: c for c in self.summary(d)["checks"]}
        self.assertEqual({n: res[n]["status"] for n, _, _ in modes},
                         {"pass": "PASS", "fail": "FAIL", "crash": "FAIL", "timeout": "FAIL"})
        self.assertLess(res["timeout"]["seconds"], 30)
        for n, _, _ in modes:
            pids = self.pids(d, n, wait=1)
            self.assertEqual(self.gone(pids, 1), [], "%s left %s" % (n, pids))
            self.assertEqual(len(res[n]["leftover"]), 2, res[n])      # named in summary.json
            self.assertIn("killed with the check's job", (d / "run" / (n + ".log")).read_text(encoding="utf-8"))
        self.assertTrue(dll_free(dll))

    def test_b_ctrl_c(self):
        d, dll = self.case("interrupt")
        p = self.run_tests(d, [self.check("hang", d, dll, "hang", 300), self.check("after", d, dll, "pass")],
                           **hidden_console())
        try:
            pids = self.pids(d, "hang")
            self.assertFalse(dll_free(dll, wait=0), "the probe is meaningless: a loaded DLL must be busy")
            time.sleep(0.5)
            # The tree ignores Ctrl+C, as a stuck simulator would; only run_tests reacts.
            r = subprocess.run([sys.executable, "-c", CTRL_C_SENDER, str(p.pid)], creationflags=DETACHED_PROCESS,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            out, _ = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 130, out)
        self.assertEqual(self.gone(pids), [])
        s = self.summary(d)
        self.assertTrue(s["interrupted"])
        self.assertEqual([c["name"] for c in s["checks"]], ["hang"])     # nothing runs after the interrupt
        self.assertFalse(s["pass"])
        self.assertTrue(dll_free(dll))

    def test_c_runner_killed(self):
        d, dll = self.case("killed")
        p = self.run_tests(d, [self.check("hang", d, dll, "hang", 300)])
        try:
            pids = self.pids(d, "hang")
            self.assertTrue(all(procjob.pid_alive(x) for x in pids))
        finally:
            p.kill()                       # TerminateProcess: no finally, no atexit runs in run_tests
            p.communicate()
        self.assertEqual(self.gone(pids), [])
        self.assertTrue(dll_free(dll))

    def test_d_tied_test_process_killed(self):
        """A test run alone (simproc ties it): killed from outside, it takes the processes it started with it."""
        d, dll = self.case("tied")
        code = ("import subprocess, sys, time; sys.path.insert(0, %r); import procjob; procjob.tie_this_process(); "
                "subprocess.Popen([sys.executable, %r, %r, %r, '0']); time.sleep(600)"
                % (str(HERE), str(self.sleeper), str(dll), str(d / "tied.pids")))
        p = subprocess.Popen([sys.executable, "-c", code])
        try:
            pids = self.pids(d, "tied")
        finally:
            p.kill()
            p.wait()
        self.assertEqual(self.gone(pids), [])
        self.assertTrue(dll_free(dll))

    def test_e_otahost_dll_free(self):
        """Under run_tests this check runs last: no process of the checks before it still holds the DLL."""
        if not OTAHOST.exists():
            self.skipTest("no %s (host_dll builds it)" % OTAHOST)
        self.assertTrue(dll_free(OTAHOST), "%s is held by a process" % OTAHOST)


if __name__ == "__main__":
    unittest.main()
