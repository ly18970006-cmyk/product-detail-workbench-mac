@echo off
setlocal
cd /d "%~dp0"
set "DETAIL_WORKBENCH=1"
if defined DETAIL_DATA_DIR if not defined DETAIL_PORT set "DETAIL_PORT=8884"
if not exist "%~dp0..\detail-page-storage.local.json" (
    if not defined DETAIL_PORT set "DETAIL_PORT=8884"
    if not defined DETAIL_DATA_DIR set "DETAIL_DATA_DIR=%~dp0.data\workbench"
)
call "%~dp0Start.cmd"
exit /b %errorlevel%
