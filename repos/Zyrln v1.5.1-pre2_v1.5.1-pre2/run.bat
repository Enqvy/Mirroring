@echo off
title Zyrln Control Center
echo Starting Zyrln...

set "ZYRLN_EXE="
if exist zyrln-windows-amd64.exe set "ZYRLN_EXE=zyrln-windows-amd64.exe"

if "%ZYRLN_EXE%"=="" (
    echo [ERROR] Zyrln executable not found!
    echo Put zyrln-windows-amd64.exe in this folder.
    pause
    exit /b
)
start "" "%ZYRLN_EXE%" -gui
echo Zyrln is running in the background. 
echo Your browser should open automatically.
echo Keep this window open while using Zyrln.
pause
