@echo off
rem PaperOrchestra tools for Claude Code (Windows cmd / PowerShell). Usage: po <command> [args]
setlocal
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PY=%PAPER_ORCHESTRA_PYTHON%"
if "%PY%"=="" if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"
"%PY%" "%~dp0cli.py" %*
exit /b %ERRORLEVEL%
