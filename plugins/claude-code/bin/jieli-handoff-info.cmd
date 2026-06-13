@echo off
setlocal
set "BIN_DIR=%~dp0"
set "PLUGIN_ROOT=%BIN_DIR%.."

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python "%PLUGIN_ROOT%\scripts\handoff_info.py" %*
) else (
  py -3 "%PLUGIN_ROOT%\scripts\handoff_info.py" %*
)
exit /b %ERRORLEVEL%
