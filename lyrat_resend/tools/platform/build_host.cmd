@echo off
rem Builds plathost.dll from the platform's plain C (rom_flasher.c, rom_bundle.c) with MSVC, for the PC tests of goal 095.
rem based on lyrat_resend\tools\ota_test\build_host.cmd. Output: tools\platform\build\plathost.dll (ignored by git).
set P=%~dp0..\..\..\platform\components\plat_core
set OUT=%~dp0build
if not exist "%OUT%" mkdir "%OUT%"
set VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat
if not "%OTA_VCVARS%"=="" set VCVARS=%OTA_VCVARS%
call "%VCVARS%" >nul
cl /nologo /W4 /WX /wd4100 /O2 /LD /I "%P%\include" "%P%\rom_flasher.c" "%P%\rom_bundle.c" "%~dp0host_shim.c" /Fo"%OUT%\\" /Fe"%OUT%\plathost.dll" /link /DEF:"%~dp0plathost.def"
