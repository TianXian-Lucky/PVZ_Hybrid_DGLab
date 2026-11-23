@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "FX=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319"
set "CSC=%FX%\csc.exe"
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "ICON=%ROOT%\app\resources\app\assets\app.ico"

if not exist "%CSC%" (
  >&2 echo .NET Framework x64 C# compiler is missing: %CSC%
  exit /b 1
)
if not exist "%ICON%" (
  >&2 echo Launcher icon is missing.
  exit /b 1
)
if not exist "%~dp0RuntimeInstaller.cs" (
  >&2 echo RuntimeInstaller.cs is missing.
  exit /b 1
)

"%CSC%" /nologo /noconfig /langversion:5 /codepage:65001 /utf8output /optimize+ ^
  /target:winexe /platform:x64 /main:Launch ^
  /win32icon:"%ICON%" /out:"%ROOT%\启动.exe" ^
  /reference:"%FX%\System.dll" /reference:"%FX%\System.Core.dll" ^
  /reference:"%FX%\System.Web.Extensions.dll" ^
  /reference:"%FX%\System.IO.Compression.dll" /reference:"%FX%\System.IO.Compression.FileSystem.dll" ^
  /reference:"%FX%\System.Windows.Forms.dll" /reference:"%FX%\System.Drawing.dll" ^
  "%~dp0Launch.cs" "%~dp0RuntimeInstaller.cs"
exit /b %ERRORLEVEL%

