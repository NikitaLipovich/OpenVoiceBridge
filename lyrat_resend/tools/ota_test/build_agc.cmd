@echo off
rem Builds agchost.dll: the firmware's main/call_agc.c with MSVC /W4 /WX (goal 113). based on build_msbc.cmd.
rem Output: tools\ota_test\build\agchost.dll (ignored by git). tools\bench\agc.py runs this when the DLL is missing.
setlocal
set M=%~dp0..\..\main
set OUT=%~dp0build\agc
if not exist "%OUT%" mkdir "%OUT%"
set VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat
if not "%OTA_VCVARS%"=="" set VCVARS=%OTA_VCVARS%
call "%VCVARS%" >nul
cl /nologo /W4 /WX /O2 /fp:strict /LD /I "%M%" "%M%\call_agc.c" "%~dp0agc_host.c" /Fo"%OUT%\\" /Fe"%~dp0build\agchost.dll"
