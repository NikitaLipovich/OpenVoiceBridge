@echo off
rem Builds otahost.dll from the firmware's shared sources (ota_link, ota_sha256, dbg_log, ctl_frame) with MSVC.
rem Needs Visual Studio Build Tools 2022 (cl). Output: tools\ota_test\build\otahost.dll (ignored by git).
set M=%~dp0..\..\main
set OUT=%~dp0build
if not exist "%OUT%" mkdir "%OUT%"
set VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat
if not "%OTA_VCVARS%"=="" set VCVARS=%OTA_VCVARS%
call "%VCVARS%" >nul
cl /nologo /W4 /WX /wd4100 /wd4996 /O2 /LD /I "%M%" "%M%\ota_link.c" "%M%\ota_sha256.c" "%M%\dbg_log.c" "%~dp0host_shim.c" /Fo"%OUT%\\" /Fe"%OUT%\otahost.dll" /link /DEF:"%~dp0otahost.def"
