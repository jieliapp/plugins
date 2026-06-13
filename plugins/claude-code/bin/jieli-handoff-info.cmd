@echo off
setlocal
set "BIN_DIR=%~dp0"
set "PLUGIN_ROOT=%BIN_DIR%.."

node "%PLUGIN_ROOT%\scripts\handoff_info.mjs" %*
exit /b %ERRORLEVEL%
