# hold_sco.ps1 — ручка W2 + измеритель G1.
# Открывает Windows capture-endpoint LyraT через winmm и держит его открытым.
# Открытие endpoint'а — это то, что заставляет Windows-AG поднять SCO к плате
# (ровно то же делает Zoom, когда выбирает микрофон гарнитуры).
# Печатает JSON-строки: {"ev":"devices"|"open"|"level"|"error"|"done", ...}
param(
    [string]$Match   = 'LyraT',
    [int]$Seconds    = 30,
    [int]$PrintMs    = 1000,
    [int]$SampleRate = 16000
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Globalization;

public static class WaveInHold {
  const int  CALLBACK_NULL = 0x00000000;
  const uint WHDR_DONE     = 0x00000001;

  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct WAVEINCAPS {
    public ushort wMid, wPid;
    public uint vDriverVersion;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szPname;
    public uint dwFormats;
    public ushort wChannels, wReserved1;
  }

  [StructLayout(LayoutKind.Sequential, Pack = 1)]
  struct WAVEFORMATEX {
    public ushort wFormatTag, nChannels;
    public uint nSamplesPerSec, nAvgBytesPerSec;
    public ushort nBlockAlign, wBitsPerSample, cbSize;
  }

  [StructLayout(LayoutKind.Sequential)]
  struct WAVEHDR {
    public IntPtr lpData;
    public uint dwBufferLength, dwBytesRecorded;
    public IntPtr dwUser;
    public uint dwFlags, dwLoops;
    public IntPtr lpNext, reserved;
  }

  [DllImport("winmm.dll")] static extern uint waveInGetNumDevs();
  [DllImport("winmm.dll", CharSet = CharSet.Unicode, EntryPoint = "waveInGetDevCapsW")]
  static extern uint waveInGetDevCaps(IntPtr id, ref WAVEINCAPS caps, uint size);
  [DllImport("winmm.dll")] static extern uint waveInOpen(out IntPtr h, uint dev, ref WAVEFORMATEX f, IntPtr cb, IntPtr inst, uint flags);
  [DllImport("winmm.dll")] static extern uint waveInPrepareHeader(IntPtr h, IntPtr hdr, uint size);
  [DllImport("winmm.dll")] static extern uint waveInUnprepareHeader(IntPtr h, IntPtr hdr, uint size);
  [DllImport("winmm.dll")] static extern uint waveInAddBuffer(IntPtr h, IntPtr hdr, uint size);
  [DllImport("winmm.dll")] static extern uint waveInStart(IntPtr h);
  [DllImport("winmm.dll")] static extern uint waveInStop(IntPtr h);
  [DllImport("winmm.dll")] static extern uint waveInReset(IntPtr h);
  [DllImport("winmm.dll")] static extern uint waveInClose(IntPtr h);

  static string J(string s) {
    return s == null ? "" : s.Replace("\\", "\\\\").Replace("\"", "\\\"");
  }
  static string F(double d) {
    return d.ToString("0.###", CultureInfo.InvariantCulture);
  }

  public static int Run(string match, int seconds, int printMs, int sampleRate) {
    uint n = waveInGetNumDevs();
    int target = -1;
    var names = new System.Text.StringBuilder();
    for (uint i = 0; i < n; i++) {
      var c = new WAVEINCAPS();
      if (waveInGetDevCaps((IntPtr)i, ref c, (uint)Marshal.SizeOf(typeof(WAVEINCAPS))) != 0) continue;
      if (names.Length > 0) names.Append(",");
      names.Append("\"" + J(c.szPname) + "\"");
      if (target < 0 && c.szPname != null &&
          c.szPname.IndexOf(match, StringComparison.OrdinalIgnoreCase) >= 0) target = (int)i;
    }
    Console.WriteLine("{\"ev\":\"devices\",\"count\":" + n + ",\"names\":[" + names + "],\"target\":" + target + "}");
    if (target < 0) {
      Console.WriteLine("{\"ev\":\"error\",\"why\":\"endpoint_not_present\"}");
      return 2;
    }

    var fmt = new WAVEFORMATEX();
    fmt.wFormatTag = 1; // PCM
    fmt.nChannels = 1;
    fmt.nSamplesPerSec = (uint)sampleRate;
    fmt.wBitsPerSample = 16;
    fmt.nBlockAlign = (ushort)(fmt.nChannels * fmt.wBitsPerSample / 8);
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;
    fmt.cbSize = 0;

    IntPtr hwi;
    uint rc = waveInOpen(out hwi, (uint)target, ref fmt, IntPtr.Zero, IntPtr.Zero, CALLBACK_NULL);
    if (rc != 0) {
      Console.WriteLine("{\"ev\":\"error\",\"why\":\"waveInOpen\",\"mmrc\":" + rc + "}");
      return 3;
    }
    Console.WriteLine("{\"ev\":\"open\",\"dev\":" + target + ",\"rate\":" + sampleRate + "}");

    const int NBUF = 8;
    int bufBytes = fmt.nBlockAlign * (sampleRate / 10); // 100 ms
    int hdrSize = Marshal.SizeOf(typeof(WAVEHDR));
    var hdrs = new IntPtr[NBUF];
    var data = new IntPtr[NBUF];
    for (int i = 0; i < NBUF; i++) {
      data[i] = Marshal.AllocHGlobal(bufBytes);
      hdrs[i] = Marshal.AllocHGlobal(hdrSize);
      var h = new WAVEHDR();
      h.lpData = data[i];
      h.dwBufferLength = (uint)bufBytes;
      Marshal.StructureToPtr(h, hdrs[i], false);
      waveInPrepareHeader(hwi, hdrs[i], (uint)hdrSize);
      waveInAddBuffer(hwi, hdrs[i], (uint)hdrSize);
    }
    waveInStart(hwi);

    var sw = System.Diagnostics.Stopwatch.StartNew();
    long lastPrint = 0;
    double acc = 0; long accN = 0; int accPeak = 0; long totalSamples = 0;
    var tmp = new byte[bufBytes];

    while (sw.Elapsed.TotalSeconds < seconds) {
      bool any = false;
      for (int i = 0; i < NBUF; i++) {
        var h = (WAVEHDR)Marshal.PtrToStructure(hdrs[i], typeof(WAVEHDR));
        if ((h.dwFlags & WHDR_DONE) == 0) continue;
        any = true;
        int got = (int)h.dwBytesRecorded;
        if (got > 0) {
          Marshal.Copy(h.lpData, tmp, 0, got);
          for (int k = 0; k + 1 < got; k += 2) {
            short s = (short)(tmp[k] | (tmp[k + 1] << 8));
            int a = s < 0 ? -s : s;
            if (a > accPeak) accPeak = a;
            acc += (double)s * s;
            accN++;
          }
          totalSamples += got / 2;
        }
        waveInUnprepareHeader(hwi, hdrs[i], (uint)hdrSize);
        h.dwFlags = 0; h.dwBytesRecorded = 0;
        Marshal.StructureToPtr(h, hdrs[i], false);
        waveInPrepareHeader(hwi, hdrs[i], (uint)hdrSize);
        waveInAddBuffer(hwi, hdrs[i], (uint)hdrSize);
      }
      if (!any) System.Threading.Thread.Sleep(10);

      long ms = sw.ElapsedMilliseconds;
      if (ms - lastPrint >= printMs) {
        double rms = accN > 0 ? Math.Sqrt(acc / accN) : 0;
        Console.WriteLine("{\"ev\":\"level\",\"t\":" + F(sw.Elapsed.TotalSeconds)
          + ",\"rms\":" + F(rms) + ",\"peak\":" + accPeak
          + ",\"samples\":" + totalSamples + "}");
        Console.Out.Flush();
        acc = 0; accN = 0; accPeak = 0;
        lastPrint = ms;
      }
    }

    waveInStop(hwi);
    waveInReset(hwi);
    for (int i = 0; i < NBUF; i++) {
      waveInUnprepareHeader(hwi, hdrs[i], (uint)hdrSize);
      Marshal.FreeHGlobal(hdrs[i]);
      Marshal.FreeHGlobal(data[i]);
    }
    waveInClose(hwi);
    Console.WriteLine("{\"ev\":\"done\",\"samples\":" + totalSamples + "}");
    return 0;
  }
}
'@

exit ([WaveInHold]::Run($Match, $Seconds, $PrintMs, $SampleRate))
