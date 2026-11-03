# _bthapi.ps1 — общий P/Invoke-слой к bthprops.cpl. Дот-сорсится другими скриптами.
if (-not ('Bth.Api' -as [type])) {
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
}

function Get-BtRadio {
    $frp = New-Object Bth.Api+BLUETOOTH_FIND_RADIO_PARAMS
    $frp.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($frp)
    $hRadio = [IntPtr]::Zero
    $hFind = [Bth.Api]::BluetoothFindFirstRadio([ref]$frp, [ref]$hRadio)
    if ($hFind -eq [IntPtr]::Zero) { return $null }
    return [pscustomobject]@{ hFind = $hFind; hRadio = $hRadio }
}

function Close-BtRadio($radio) {
    if ($radio) { [void][Bth.Api]::BluetoothFindRadioClose($radio.hFind) }
}

function Get-BtDeviceInfo {
    param([IntPtr]$hRadio, [string]$Match)
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

    $found = $null
    $hFind = [Bth.Api]::BluetoothFindFirstDevice([ref]$sp, [ref]$info)
    if ($hFind -ne [IntPtr]::Zero) {
        do {
            if ($info.szName -like "*$Match*") { $found = $info; break }
            $info = New-Object Bth.Api+BLUETOOTH_DEVICE_INFO
            $info.dwSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
        } while ([Bth.Api]::BluetoothFindNextDevice($hFind, [ref]$info))
        [void][Bth.Api]::BluetoothFindDeviceClose($hFind)
    }
    if ($null -eq $found) { return $null }
    return [pscustomobject]@{ Info = $found }
}
