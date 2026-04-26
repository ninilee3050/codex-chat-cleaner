@echo off
setlocal
set "PY=C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0codex_chat_cleaner.py"
if errorlevel 1 (
  echo.
  echo Codex Chat Cleaner failed.
  pause
)
endlocal
