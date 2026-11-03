# bt_unpair.ps1 — ручка M1: снять одностороннюю пару.
#
# Симптом, который она чинит: Windows считает устройство сопряжённым
# (fAuthenticated/fRemembered = true), а bond list на плате пуст — плата про
# этот ПК не знает. Такой линк не поднимется ни с одной стороны: Windows не
# инициирует (он AG), а плата не может — ей некуда и нечем аутентифицироваться.
#
# Скрипт только УДАЛЯЕТ пару. Сопрягать заново нужно руками:
#   Параметры -> Bluetooth -> Добавить устройство -> LyraT_Phone
# Плата уже discoverable и авто-принимает SSP/PIN 0000.
param(
    [string]$Match = 'LyraT',
    [switch]$Confirm
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_bthapi.ps1"

if (-not ('Bth.Remove' -as [type])) {
Add-Type -Namespace Bth -Name Remove -MemberDefinition @'
[DllImport("bthprops.cpl", SetLastError = true)]
public static extern uint BluetoothRemoveDevice(ref ulong pAddress);
'@
}

$radio = Get-BtRadio
if (-not $radio) { '{"ok":false,"error":"no_bt_radio"}'; exit 1 }

$dev = Get-BtDeviceInfo -hRadio $radio.hRadio -Match $Match
Close-BtRadio $radio

if (-not $dev) {
    "{`"ok`":false,`"error`":`"device_not_found`",`"match`":`"$Match`"}"
    exit 2
}

$info = $dev.Info
$addr = $info.Address
$hex  = ('{0:X12}' -f $addr)

if (-not $Confirm) {
    [pscustomobject]@{
        ok        = $false
        dry_run   = $true
        device    = $info.szName
        address   = $hex
        connected = [bool]$info.fConnected
        note      = 'Запустить с -Confirm, чтобы реально снять пару. Потом спарить заново вручную.'
    } | ConvertTo-Json -Compress
    exit 0
}

$rc = [Bth.Remove]::BluetoothRemoveDevice([ref]$addr)

[pscustomobject]@{
    ok      = ($rc -eq 0)
    rc      = $rc
    device  = $info.szName
    address = $hex
    next    = 'Параметры -> Bluetooth -> Добавить устройство -> LyraT_Phone'
} | ConvertTo-Json -Compress
