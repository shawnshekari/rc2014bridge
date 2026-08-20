@echo off
rem Start the rc2014bridge GUI + MCP server (Windows).
rem Usage: bridge.cmd [serial-port] [baud] [extra app args...]
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=%RC2014_PORT%"
if "%PORT%"=="" (
  echo bridge: no serial port given; pass one: bridge.cmd COM3  [baud]  (or set RC2014_PORT) 1>&2
  exit /b 1
)

set "BAUD=%~2"
if "%BAUD%"=="" set "BAUD=%RC2014_BAUD%"
if "%BAUD%"=="" set "BAUD=115200"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" if defined RC2014_VENV set "PY=%RC2014_VENV%\Scripts\python.exe"
if not exist "%PY%" (
  echo bridge: no venv python found; run: python -m venv .venv ^& .venv\Scripts\pip install -r requirements.txt 1>&2
  exit /b 1
)

shift & shift
"%PY%" -m rc2014bridge.app --port %PORT% --baud %BAUD% --mcp-host 127.0.0.1 %1 %2 %3 %4 %5 %6 %7 %8 %9
