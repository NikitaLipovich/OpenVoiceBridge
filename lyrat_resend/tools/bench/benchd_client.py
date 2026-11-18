"""The client side of benchd (goal 103): is the bench service running, where is its API, where its log relay, who holds
the bench lock. Used by udplog.py, console.LogHub, s3_uart.Channel, ota.find_host, owner_common.bench_busy and bench.py.

The service is looked up without a network call when it is not there: benchd writes bench_runs/benchd/run-<port>.json
(pid, ports, S3 address) while it runs; no file, or its pid gone, means no service (a refused connection to localhost
costs about 2 s on Windows). Rules:
  BENCH_NO_BENCHD=1       never use the service (the test runner sets it, apart from the tests of the service)
  BENCHD_URL=http://h:p   the service is there (no file needed)
  otherwise               run-<5080 + BENCH_PORT_OFFSET>.json, its pid alive, then GET /status
When BENCH_S3_ADDR is set and differs from the service's S3, the service is not used for the log and the address: a
test on the simulator beside a running bench service stays on its simulator.

As a module: info(), s3_addr(), log_endpoint(host), call(method, path, body), lock_state(), my_lock(state),
acquire(owner, ...), release(...), renew(...), pid_alive(pid), Lock (a context manager).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import RUNS_ROOT, bench_port  # noqa: E402

HTTP_PORT = 5080
RELAY_PORT = 5081
DISCOVERY_PORT = 5082       # benchd --lan answers 'BENCHD?' here (goal 104)
CACHE_S = 10.0
_cache = {"at": None, "info": None, "key": None}


def home():
    """Folder of the service's files: logs, events, state, the run file."""
    return Path(os.environ.get("BENCHD_HOME") or (RUNS_ROOT / "benchd"))


def run_file(port=None):
    return home() / ("run-%d.json" % (port or bench_port(HTTP_PORT)))


def pid_alive(pid):
    """True while the process runs. On Windows through OpenProcess: os.kill(pid, 0) there would end the process."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
            return bool(ok) and code.value == 259           # STILL_ACTIVE
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def base_url():
    """The service's URL when it should be there, else None. No network call."""
    if os.environ.get("BENCH_NO_BENCHD") == "1":
        return None
    url = os.environ.get("BENCHD_URL")
    if url:
        return url.rstrip("/")
    try:
        run = json.loads(run_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not pid_alive(run.get("pid")):
        return None
    return "http://127.0.0.1:%d" % run["http_port"]


def call(method, path, body=None, timeout=5.0, url=None):
    """One API call: (HTTP status, JSON answer). (None, {"error": ...}) when the service does not answer."""
    url = url or base_url()
    if not url:
        return None, {"error": "benchd is not running"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-Bench", "1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {"error": str(e)}
    except (OSError, ValueError) as e:
        return None, {"error": str(e)}


def info(fresh=False):
    """The service's /status, cached CACHE_S seconds; None when it does not run."""
    url = base_url()
    if not url:
        return None
    now = time.monotonic()
    if not fresh and _cache["key"] == url and _cache["at"] is not None and now - _cache["at"] < CACHE_S:
        return _cache["info"]
    code, st = call("GET", "/status", timeout=2.0, url=url)
    _cache.update(at=now, key=url, info=st if code == 200 else None)
    return _cache["info"]


def _usable_for(host=None):
    """The service's status when it serves this S3 (or any, without one asked), else None."""
    st = info()
    if not st:
        return None
    s3 = (st.get("s3") or {}).get("addr")
    want = host or os.environ.get("BENCH_S3_ADDR")
    if want and s3 and want != s3:
        return None
    return st


def s3_addr():
    """The S3's address as the service knows it, or None."""
    st = _usable_for()
    return ((st or {}).get("s3") or {}).get("addr")


def log_endpoint(host=None):
    """(host, port) to ask for the log with LOG1: the service's relay when it runs for this S3, else None."""
    if os.environ.get("BENCH_LOG_DIRECT") == "1":
        return None
    st = _usable_for(host)
    if not st or not st.get("relay_port"):
        return None
    return "127.0.0.1", int(st["relay_port"])


# --- the bench lock ------------------------------------------------------------------------------------------------

def lock_state():
    """The lock as the service has it ({"holder": None|{owner, until, ...}, "queue": [...]}), None without a service."""
    if not base_url():
        return None
    code, st = call("GET", "/lock", timeout=2.0)
    return st if code == 200 else None


def my_lock(state):
    """True when the lock is held by this process's goal (BENCH_LOCK_TOKEN or BENCH_LOCK_OWNER)."""
    holder = (state or {}).get("holder")
    if not holder:
        return False
    token = os.environ.get("BENCH_LOCK_TOKEN")
    owner = os.environ.get("BENCH_LOCK_OWNER")
    return bool((owner and holder.get("owner") == owner) or (token and holder.get("token_hint") == token[:6]))


def acquire(owner, ttl_s=900, wait_s=0, pid=None, note=""):
    body = {"owner": owner, "ttl_s": ttl_s, "wait_s": wait_s, "pid": pid, "note": note}
    return call("POST", "/lock", body, timeout=wait_s + 10.0)


def renew(owner, token, ttl_s=None):
    return call("POST", "/lock/renew", {"owner": owner, "token": token, "ttl_s": ttl_s})


def release(owner, token=None):
    return call("POST", "/lock/release", {"owner": owner, "token": token})


class Lock:
    """with Lock("104", ttl_s=600, wait_s=60): ... takes the bench lock (renewed by a thread), sets BENCH_LOCK_OWNER and
    BENCH_LOCK_TOKEN for the children, gives it back at the end. Raises RuntimeError when it cannot be had. Without a
    running service it does nothing (the bench has no lock then)."""

    def __init__(self, owner, ttl_s=900, wait_s=0, note=""):
        self.owner, self.ttl_s, self.wait_s, self.note = owner, ttl_s, wait_s, note
        self.token = None
        self._stop = None

    def __enter__(self):
        if not base_url():
            return self
        code, ans = acquire(self.owner, self.ttl_s, self.wait_s, os.getpid(), self.note)
        if code != 200:
            raise RuntimeError("bench lock: %s" % (ans.get("error") or ans))
        self.token = ans["token"]
        os.environ["BENCH_LOCK_OWNER"] = self.owner
        os.environ["BENCH_LOCK_TOKEN"] = self.token
        import threading
        self._stop = threading.Event()

        def keep():
            while not self._stop.wait(max(5.0, self.ttl_s / 3.0)):
                renew(self.owner, self.token, self.ttl_s)
        threading.Thread(target=keep, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self.token:
            self._stop.set()
            release(self.owner, self.token)
            os.environ.pop("BENCH_LOCK_TOKEN", None)
            os.environ.pop("BENCH_LOCK_OWNER", None)
            self.token = None
