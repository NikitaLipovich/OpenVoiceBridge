@echo off
rem Builds msbchost.dll: the firmware's main/msbc_codec.c with the Bluedroid SBC codec of ESP-IDF, with MSVC (goal 112).
rem Needs Visual Studio Build Tools 2022 (cl) and ESP-IDF (IDF_PATH, else C:\esp\v5.5.2\esp-idf).
rem Output: tools\ota_test\build\msbchost.dll (ignored by git). tools\bench\msbc.py runs this when the DLL is missing.
setlocal
set M=%~dp0..\..\main
set OUT=%~dp0build\msbc
if "%IDF_PATH%"=="" set IDF_PATH=C:\esp\v5.5.2\esp-idf
set SBC=%IDF_PATH%\components\bt\host\bluedroid\external\sbc
if not exist "%OUT%" mkdir "%OUT%"
set VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat
if not "%OTA_VCVARS%"=="" set VCVARS=%OTA_VCVARS%
call "%VCVARS%" >nul
cl /nologo /W1 /O2 /LD /I "%~dp0msbc_shim" /I "%M%" /I "%SBC%\decoder\include" /I "%SBC%\encoder\include" ^
  "%M%\msbc_codec.c" "%~dp0msbc_host.c" "%SBC%\decoder\srce\*.c" "%SBC%\encoder\srce\*.c" ^
  /Fo"%OUT%\\" /Fe"%~dp0build\msbchost.dll"
