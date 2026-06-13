@echo off
setlocal
set "BIN_DIR=%~dp0"
set "PLUGIN_ROOT=%BIN_DIR%.."

node "%PLUGIN_ROOT%\scripts\read_thread.mjs" %*
exit /b %ERRORLEVEL%
