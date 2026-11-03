# probe_win.ps1 — снимок Windows-стороны: BT-устройства + состояния аудио-endpoint'ов.
# Печатает один JSON-объект в stdout. Ничего не меняет.
param([string]$Match = 'LyraT')

$ErrorActionPreference = 'Stop'

Add-Type -Namespace Bth -Name Api -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct BLUETOOTH_FIND_RADIO_PARAMS { public uint dwSize; }

[StructLayout(LayoutKind.Sequential)]
public struct SYSTEMTIME {
  public ushort wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMilliseconds;
}

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct BLUETOOTH_DEVICE_INFO {
  public uint dwSize;
  public ulong Address;
  public uint ulClassofDevice;
  [MarshalAs(UnmanagedType.Bool)] public bool fConnected;
  [MarshalAs(UnmanagedType.Bool)] public bool fRemembered;
  [MarshalAs(UnmanagedType.Bool)] public bool fAuthenticated;
  public SYSTEMTIME stLastSeen;
  public SYSTEMTIME stLastUsed;
  [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 248)] public string szName;
}

[StructLayout(LayoutKind.Sequential)]
public struct BLUETOOTH_DEVICE_SEARCH_PARAMS {
  public uint dwSize;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnAuthenticated;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnRemembered;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnUnknown;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnConnected;
  [MarshalAs(UnmanagedType.Bool)] public bool fIssueInquiry;
  public byte cTimeoutMultiplier;
  public IntPtr hRadio;
}

[DllImport("bthprops.cpl", SetLastError = true)]
public static extern IntPtr BluetoothFindFirstRadio(ref BLUETOOTH_FIND_RADIO_PARAMS p, out IntPtr hRadio);
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern bool BluetoothFindRadioClose(IntPtr hFind);
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern IntPtr BluetoothFindFirstDevice(ref BLUETOOTH_DEVICE_SEARCH_PARAMS p, ref BLUETOOTH_DEVICE_INFO info);
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern bool BluetoothFindNextDevice(IntPtr hFind, ref BLUETOOTH_DEVICE_INFO info);
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern bool BluetoothFindDeviceClose(IntPtr hFind);
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern uint BluetoothSetServiceState(IntPtr hRadio, ref BLUETOOTH_DEVICE_INFO info, ref Guid guid, uint dwServiceFlags);
'@

function Get-BtDevices {
    $frp = New-Object Bth.Api+BLUETOOTH_FIND_RADIO_PARAMS
    $frp.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($frp)
    $hRadio = [IntPtr]::Zero
    $hFindRadio = [Bth.Api]::BluetoothFindFirstRadio([ref]$frp, [ref]$hRadio)
    if ($hFindRadio -eq [IntPtr]::Zero) { return @() }

    $sp = New-Object Bth.Api+BLUETOOTH_DEVICE_SEARCH_PARAMS
    $sp.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($sp)
    $sp.fReturnAuthenticated = $true
    $sp.fReturnRemembered    = $true
    $sp.fReturnConnected     = $true
    $sp.fReturnUnknown       = $false
    $sp.fIssueInquiry        = $false
    $sp.cTimeoutMultiplier   = 0
    $sp.hRadio               = $hRadio

    $info = New-Object Bth.Api+BLUETOOTH_DEVICE_INFO
    $info.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)

    $out = @()
    $hFind = [Bth.Api]::BluetoothFindFirstDevice([ref]$sp, [ref]$info)
    if ($hFind -ne [IntPtr]::Zero) {
        do {
            $out += [pscustomobject]@{
                name          = $info.szName
                address       = ('{0:X12}' -f $info.Address)
                cod           = $info.ulClassofDevice
                connected     = [bool]$info.fConnected
                authenticated = [bool]$info.fAuthenticated
                remembered    = [bool]$info.fRemembered
            }
            $info = New-Object Bth.Api+BLUETOOTH_DEVICE_INFO
            $info.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
        } while ([Bth.Api]::BluetoothFindNextDevice($hFind, [ref]$info))
        [void][Bth.Api]::BluetoothFindDeviceClose($hFind)
    }
    [void][Bth.Api]::BluetoothFindRadioClose($hFindRadio)
    return $out
}

function Get-Endpoints {
    $res = @()
    foreach ($flow in 'Capture', 'Render') {
        $root = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\$flow"
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $props = Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue
            $desc  = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
            $name  = $props.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
            if ("$desc$name" -like "*$Match*") {
                $res += [pscustomobject]@{
                    flow   = $flow
                    id     = $_.PSChildName
                    device = $desc
                    name   = $name
                    state  = [int](Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DeviceState
                }
            }
        }
    }
    return $res
}

$devs = @(Get-BtDevices | Where-Object { $_.name -like "*$Match*" })
$eps  = @(Get-Endpoints)
$cap  = @($eps | Where-Object { $_.flow -eq 'Capture' })

[pscustomobject]@{
    ts                = (Get-Date).ToString('o')
    match             = $Match
    bt_devices        = $devs
    bt_connected      = [bool](@($devs | Where-Object { $_.connected }).Count)
    endpoints         = $eps
    win_capture_state = $(if ($cap.Count) { ($cap | Sort-Object state | Select-Object -First 1).state } else { -1 })
    capture_active    = [bool](@($cap | Where-Object { $_.state -eq 1 }).Count)
    stale_endpoints   = @($eps | Where-Object { $_.state -eq 4 } | Select-Object -ExpandProperty id)
} | ConvertTo-Json -Depth 6 -Compress
