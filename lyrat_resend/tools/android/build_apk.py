#!/usr/bin/env python3
"""Build the SCO probe APK with plain SDK tools: javac, d8, aapt2, zipalign, apksigner. No Gradle.

  build_apk.py [--install] [--out DIR] [--sdk DIR] [--jdk DIR] [--build-tools VER]

Output goes to <runs>/android/ (outside git): scoprobe.apk and debug.keystore. The key is a throwaway
debug key created on first use and reused later, so that `adb install -r` keeps working.
SDK: --sdk, ANDROID_HOME, ANDROID_SDK_ROOT or %LOCALAPPDATA%/Android/Sdk.
JDK: --jdk, JAVA_HOME or the JBR of Android Studio.
Exit code: 0 built (and installed with --install), 1 a tool failed, 2 a tool is missing.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
from benchlib import RUNS_ROOT  # noqa: E402

APP_DIR = Path(__file__).resolve().parent / "sco_probe"
PLATFORM = "android-33"
MIN_SDK, TARGET_SDK = "31", "33"
KEY_PASS = "android"      # the conventional password of an Android debug key, protects nothing


class Missing(Exception):
    pass


def find_sdk(arg):
    candidates = [arg, os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"))
    for c in candidates:
        if c and (Path(c) / "platforms" / PLATFORM / "android.jar").is_file():
            return Path(c)
    raise Missing("Android SDK with platform %s not found, use --sdk" % PLATFORM)


def find_jdk(arg):
    for c in (arg, os.environ.get("JAVA_HOME"), r"C:\Program Files\Android\Android Studio\jbr"):
        if c and any((Path(c) / "bin" / name).is_file() for name in ("javac.exe", "javac")):
            return Path(c)
    raise Missing("JDK not found, use --jdk or JAVA_HOME")


def tool(folder, name):
    for suffix in (".exe", ".bat", ""):
        path = Path(folder) / (name + suffix)
        if path.is_file():
            return str(path)
    raise Missing("%s not found in %s" % (name, folder))


def find_build_tools(sdk, wanted):
    root = sdk / "build-tools"
    if wanted:
        versions = [root / wanted]
    else:
        # Numeric order: "36.1.0" must beat "9.0.0", and a preview name must not crash the sort.
        def key(p):
            return [int(x) if x.isdigit() else -1 for x in p.name.split(".")]
        versions = sorted((p for p in root.iterdir() if p.is_dir()), key=key, reverse=True) if root.is_dir() else []
    for v in versions:
        if (v / "aapt2.exe").is_file() or (v / "aapt2").is_file():
            return v
    raise Missing("build-tools with aapt2 not found in %s" % root)


def run(cmd, env):
    print("+ " + " ".join(Path(c).name if i == 0 else str(c) for i, c in enumerate(cmd)), flush=True)
    res = subprocess.run([str(c) for c in cmd], env=env, capture_output=True, text=True, errors="replace")
    if res.stdout.strip():
        print(res.stdout.strip())
    if res.stderr.strip():
        print(res.stderr.strip(), file=sys.stderr)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd[0])


def ensure_key(path, jdk, env):
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    run([tool(jdk / "bin", "keytool"), "-genkeypair", "-keystore", path, "-storepass", KEY_PASS, "-keypass", KEY_PASS,
         "-alias", "debug", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-dname", "CN=Android Debug,O=Android,C=US"], env)


def build(args):
    sdk = find_sdk(args.sdk)
    jdk = find_jdk(args.jdk)
    bt = find_build_tools(sdk, args.build_tools)
    android_jar = sdk / "platforms" / PLATFORM / "android.jar"
    out = Path(args.out) if args.out else RUNS_ROOT / "android"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, JAVA_HOME=str(jdk))
    env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")
    sources = sorted(str(p) for p in (APP_DIR / "src").rglob("*.java"))
    if not sources:
        raise Missing("no sources in %s" % APP_DIR)
    key = out / "debug.keystore"
    ensure_key(key, jdk, env)

    with tempfile.TemporaryDirectory(dir=str(out), prefix="build-") as tmp:
        tmp = Path(tmp)
        classes, dex = tmp / "classes", tmp / "dex"
        classes.mkdir()
        dex.mkdir()
        run([tool(jdk / "bin", "javac"), "-source", "8", "-target", "8", "-Xlint:-options", "-encoding", "UTF-8",
             "-bootclasspath", android_jar, "-d", classes, *sources], env)
        run([tool(bt, "d8"), "--release", "--lib", android_jar, "--min-api", MIN_SDK, "--output", dex,
             *sorted(classes.rglob("*.class"))], env)
        base = tmp / "base.apk"
        run([tool(bt, "aapt2"), "link", "-o", base, "-I", android_jar, "--manifest", APP_DIR / "AndroidManifest.xml",
             "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
             "--version-code", "1", "--version-name", "1.0"], env)
        with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
            z.write(dex / "classes.dex", "classes.dex")
        aligned = tmp / "aligned.apk"
        run([tool(bt, "zipalign"), "-f", "4", base, aligned], env)
        run([tool(bt, "apksigner"), "sign", "--ks", key, "--ks-pass", "pass:" + KEY_PASS, "--key-pass", "pass:" + KEY_PASS,
             "--out", tmp / "signed.apk", aligned], env)
        run([tool(bt, "apksigner"), "verify", tmp / "signed.apk"], env)
        apk = out / "scoprobe.apk"
        shutil.copyfile(tmp / "signed.apk", apk)
    print("built %s (%d bytes)" % (apk, apk.stat().st_size))
    if args.install:
        # -r keeps the data, -g grants the runtime permissions from the manifest.
        run(["adb", "install", "-r", "-g", apk], env)
        print("installed")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="install on the phone with adb after the build")
    ap.add_argument("--out", help="output directory, default <runs>/android")
    ap.add_argument("--sdk")
    ap.add_argument("--jdk")
    ap.add_argument("--build-tools", help="version folder, default the newest")
    args = ap.parse_args()
    try:
        return build(args)
    except Missing as e:
        print("missing: %s" % e, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        print("failed: %s exit %s" % (Path(str(e.cmd)).name, e.returncode), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
