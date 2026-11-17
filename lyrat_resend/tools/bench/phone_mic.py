"""Record the LyraT microphone on the phone only, through Bluetooth, without the S3.

What it is for: to hear the board's mic with the S3-ETH wires taken off the LyraT, so the S3 cannot add anything.
The phone opens the headset channel with the probe app (no call), records what the LyraT sends, closes it and pulls
the file to the PC. Nothing is played on the PC (R15). Needs only adb and the phone paired with LyraT_Phone.

  python phone_mic.py --label with-s3       # optional: --set-mode off first, while the S3 wires are still on
  python phone_mic.py --label without-s3

--set-mode sends "ns <mode>" through the S3; it stays on the board until a reboot. Without it the board keeps its mode.
Output: goals_harness/bench_runs/phone_mic/<stamp>-<label>/phone.wav and result.json with the quiet-part level.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import round as rnd  # noqa: E402
import voice_check as vc  # noqa: E402
from benchlib import RUNS_ROOT, stamp  # noqa: E402


def level(path, speak_s):
    import numpy as np
    x, rate = vc.audio_check.read_wav(path)
    x = np.asarray(x, dtype=float)
    speak = x[: int(speak_s * rate)]
    quiet = x[int((speak_s + 1) * rate):]
    b = int(0.1 * rate)
    blocks = np.sqrt((quiet[: len(quiet) // b * b].reshape(-1, b) ** 2).mean(1)) if len(quiet) >= b else np.array([0.0])
    rms = lambda v: round(float(np.sqrt(np.mean(v * v))), 1) if len(v) else None  # noqa: E731
    return {"speech_rms": rms(speak), "quiet_rms": rms(quiet), "quiet_floor10": round(float(np.percentile(blocks, 10)), 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="")
    ap.add_argument("--speak", type=int, default=15)
    ap.add_argument("--quiet", type=int, default=10)
    ap.add_argument("--set-mode", choices=list(vc.ALL_MODES), help="set the board's noise mode through the S3 first")
    args = ap.parse_args()
    out = RUNS_ROOT / "phone_mic" / (stamp() + ("-" + args.label if args.label else ""))
    out.mkdir(parents=True, exist_ok=True)
    print("Папка: %s" % out, flush=True)
    if args.set_mode:
        import s3_uart
        ch = s3_uart.Channel()
        if not ch.find():
            print("S3 не найдена: режим не поставлен. Запустите без --set-mode.")
            return 2
        vc.set_mode(ch, args.set_mode)
        print("Режим платы: %s" % args.set_mode, flush=True)
    tag = time.strftime("%H%M%S")
    seconds = args.speak + args.quiet
    vc.open_channel(rnd, tag)
    try:
        print("Канал открыт. Молчите 3 с.", flush=True)
        time.sleep(3)
        rec_id = vc.phone_record_start(rnd, "phone_mic.wav", seconds, tag)
        print(">>> ГОВОРИТЕ %d с" % args.speak, flush=True)
        time.sleep(args.speak)
        print(">>> МОЛЧИТЕ %d с" % args.quiet, flush=True)
        time.sleep(args.quiet)
        vc.phone_record_end(rnd, rec_id, "phone_mic.wav", seconds, out / "phone.wav")
    finally:
        vc.close_channel(rnd, tag)
    result = {"label": args.label, "mode_set": args.set_mode, **level(out / "phone.wav", args.speak)}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Речь %s, тишина %s (10-й перцентиль %s)" % (result["speech_rms"], result["quiet_rms"], result["quiet_floor10"]))
    print("Файл: %s" % (out / "phone.wav"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
