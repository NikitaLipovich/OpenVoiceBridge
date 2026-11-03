# bt_connect.ps1 — ручка W1.
# Включает службу Hands-Free Telephony (0x111E) на устройстве и инициирует коннект.
# Эквивалент галки «Hands-Free Telephony» в Свойствах BT-устройства + кнопки «Подключиться».
param(
    [string]$Match  = 'LyraT',
    [ValidateSet('enable','disable','cycle')][string]$Action = 'enable'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_bthapi.ps1"

$BTH_SERVICE_DISABLE = 0x00
$BTH_SERVICE_ENABLE  = 0x01

$radio = Get-BtRadio
if (-not $radio) { '{"ok":false,"error":"no_bt_radio"}'; exit 1 }

$dev = Get-BtDeviceInfo -hRadio $radio.hRadio -Match $Match
if (-not $dev) {
    Close-BtRadio $radio
    "{`"ok`":false,`"error`":`"device_not_found`",`"match`":`"$Match`"}"
    exit 2
}

$info = $dev.Info
$results = @()

function Set-State([uint32]$flag, [string]$label) {
    $guids = @(
        @{ n = 'Handsfree';        g = [Guid]'0000111E-0000-1000-8000-00805F9B34FB' },
        @{ n = 'HandsfreeAudioGw'; g = [Guid]'0000111F-0000-1000-8000-00805F9B34FB' }
    )
    foreach ($e in $guids) {
        $g = $e.g
        $rc = [Bth.Api]::BluetoothSetServiceState($radio.hRadio, [ref]$script:info, [ref]$g, $flag)
        $script:results += [pscustomobject]@{ action = $label; service = $e.n; rc = $rc; ok = ($rc -eq 0) }
    }
}

if ($Action -eq 'cycle') {
    Set-State $BTH_SERVICE_DISABLE 'disable'
    Start-Sleep -Milliseconds 1200
    Set-State $BTH_SERVICE_ENABLE 'enable'
} elseif ($Action -eq 'disable') {
    Set-State $BTH_SERVICE_DISABLE 'disable'
} else {
    Set-State $BTH_SERVICE_ENABLE 'enable'
}

# Дать стеку время поднять SLC
Start-Sleep -Seconds 3
$after = Get-BtDeviceInfo -hRadio $radio.hRadio -Match $Match
Close-BtRadio $radio

[pscustomobject]@{
    ok        = [bool](@($results | Where-Object { $_.ok }).Count)
    device    = $info.szName
    address   = ('{0:X12}' -f $info.Address)
    before    = [bool]$info.fConnected
    after     = [bool]$after.Info.fConnected
    calls     = $results
} | ConvertTo-Json -Depth 5 -Compress
