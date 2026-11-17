"""Start and stop s3_sim.py for the tests; images come from OTA_TEST_BUILDS when set, else synthetic ones. Goal 092."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
BENCH = TOOLS / "bench"
sys.path.insert(0, str(HERE))
import images  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t092_"))
TOKEN = TMP / "token.h"
TOKEN.write_text('#define OTA_TOKEN "%s"\n' % os.urandom(12).hex(), encoding="utf-8")


def image_dir():
    """OTA_TEST_BUILDS if it holds the four images, else a folder of synthetic ones made once per process."""
    env = os.environ.get("OTA_TEST_BUILDS")
    if env and (Path(env) / "s3-base" / "s3_eth_bridge.bin").is_file():
        return Path(env)
    d = TMP / "images"
    if not (d / "s3-base").is_dir():
        images.prepare(d)
    return d


def image(board, kind):
    name = images.BOARDS[board][2]
    return image_dir() / ("%s-%s" % (board, kind)) / name


class Sim:
    def __init__(self, *extra):
        self.p = subprocess.Popen([sys.executable, str(HERE / "s3_sim.py"), "--token-file", str(TOKEN),
                                   "--s3-image", str(image("s3", "base")), "--lyrat-image", str(image("lyrat", "base")),
                                   *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.p.stdout.readline()
        if not line:
            err = self.p.stderr.read()
            raise RuntimeError("s3_sim did not start: %s" % err[-2000:])
        self.ports = json.loads(line)
        self.host = self.ports["host"]

    def close(self):
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def env():
    e = dict(os.environ)
    e["BENCH_S3_ADDR"] = "127.0.0.1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def run_tool(script, *args, timeout=300, extra_env=None):
    """A bench tool as a process; returns (exit code, stdout, stderr, seconds)."""
    t0 = time.monotonic()
    e = env()
    e.update(extra_env or {})
    r = subprocess.run([sys.executable, str(BENCH / script), *args], capture_output=True, text=True, timeout=timeout,
                       env=e, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr, round(time.monotonic() - t0, 1)


def last_json(text):
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def ask(port, text, host="127.0.0.1", timeout=1.0, want=1):
    """Send one datagram, collect up to want replies."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(timeout)
    out = []
    try:
        s.sendto(text.encode() if isinstance(text, str) else text, (host, port))
        while len(out) < want:
            try:
                out.append(s.recvfrom(4096)[0])
            except (socket.timeout, ConnectionResetError):
                break
    finally:
        s.close()
    return out
