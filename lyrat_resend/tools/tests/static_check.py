"""Static checks of the repository: no secret in git, the ignore rules cover the secret files, the S3 builds for Wi-Fi.

  python static_check.py           JSON of the checks; exit code 0 when all passed

  not_tracked    the secret and local files are not in git (git ls-files)
  ignored        .gitignore covers them (git check-ignore), also when they do not exist yet
  values         no value of the ignored secret files appears in any tracked file (git grep -F, working tree and index);
                 the values go to git through a temporary file that is removed, and are never printed
  net_default    s3_eth_bridge/main/main.c defaults to S3_NET_ETH 0 (Wi-Fi) and no build file sets it to 1
Made by goal 092.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
SECRET_FILES = ("s3_eth_bridge/main/ota_secrets.h", "lyrat_resend/main/wifi_secrets.h",
                "platform/u3_relay/main/plat_secrets.h")
LOCAL_FILES = SECRET_FILES + ("lyrat_resend/main/bench_variant.h",)
MIN_VALUE = 6       # shorter values (an empty password, "1") would match by chance


def git(*args, check=False):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=check)


def secret_values():
    """Every string #define of the existing secret files."""
    values = []
    for rel in SECRET_FILES:
        p = REPO / rel
        if p.exists():
            for m in re.finditer(r'^\s*#define\s+\w+\s+"((?:[^"\\]|\\.)*)"', p.read_text(encoding="utf-8"), re.M):
                if len(m.group(1)) >= MIN_VALUE:
                    values.append(m.group(1))
    return values


def check_not_tracked():
    tracked = set(git("ls-files").stdout.splitlines())
    bad = [f for f in LOCAL_FILES if f in tracked]
    return {"pass": not bad, "tracked": bad}


def check_ignored():
    r = git("check-ignore", "--no-index", *LOCAL_FILES)
    covered = set(r.stdout.splitlines())
    missing = [f for f in LOCAL_FILES if f not in covered]
    return {"pass": not missing, "not_ignored": missing}


def check_values():
    values = secret_values()
    if not values:
        return {"pass": True, "values": 0, "note": "no secret file on this PC: nothing to look for"}
    fd, path = tempfile.mkstemp(prefix="t092_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(values) + "\n")
        hits = set()
        for extra in ((), ("--cached",)):
            r = git("grep", "-F", "-l", "-I", *extra, "-f", path)
            hits.update(r.stdout.splitlines())
    finally:
        os.remove(path)
    return {"pass": not hits, "values": len(values), "files_with_a_value": sorted(hits)}


def check_net_default():
    main = (REPO / "s3_eth_bridge" / "main" / "main.c").read_text(encoding="utf-8")
    default = re.search(r"#ifndef S3_NET_ETH\s*\n#define S3_NET_ETH (\d)", main)
    setters = []
    for rel in ("s3_eth_bridge/CMakeLists.txt", "s3_eth_bridge/main/CMakeLists.txt", "s3_eth_bridge/sdkconfig.defaults"):
        if re.search(r"S3_NET_ETH\s*=?\s*1", (REPO / rel).read_text(encoding="utf-8")):
            setters.append(rel)
    return {"pass": bool(default) and default.group(1) == "0" and not setters,
            "default": default.group(1) if default else None, "set_to_1_in": setters}


def run():
    checks = {"not_tracked": check_not_tracked(), "ignored": check_ignored(), "values": check_values(),
              "net_default": check_net_default()}
    return {"pass": all(c["pass"] for c in checks.values()), "checks": checks}


def main():
    r = run()
    print(json.dumps(r, indent=1))
    return 0 if r["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
