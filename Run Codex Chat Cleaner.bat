@echo off
setlocal
set "SCRIPT=%~dp0codex_chat_cleaner.py"
set "PYW=C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
set "PY=C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%PYW%" (
  start "" "%PYW%" "%SCRIPT%"
  exit /b
)

if exist "%PY%" (
  start "Codex Chat Cleaner" /min "%PY%" "%SCRIPT%"
  exit /b
)

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  start "" pythonw.exe "%SCRIPT%"
  exit /b
)

start "Codex Chat Cleaner" /min python "%SCRIPT%"
endlocal
