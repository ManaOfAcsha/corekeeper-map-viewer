@echo off
rem Run without building: needs Python 3.9+ (py launcher). Extra options are passed through, e.g. --lan
cd /d "%~dp0"
py -3 app.py %*
