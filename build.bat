@echo off
rem Builds dist\CoreKeeperMapViewer\CoreKeeperMapViewer.exe (+ a zip) in a local virtual environment.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || goto :fail
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements-build.txt || goto :fail
.venv\Scripts\python.exe -m unittest discover -s tests -q || goto :fail
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean CoreKeeperMapViewer.spec || goto :fail
copy /y README.md dist\CoreKeeperMapViewer\ >nul
copy /y README.ko.md dist\CoreKeeperMapViewer\ >nul
copy /y LICENSE dist\CoreKeeperMapViewer\LICENSE.txt >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\CoreKeeperMapViewer -DestinationPath dist\CoreKeeperMapViewer-windows.zip" || goto :fail
echo.
echo Built: dist\CoreKeeperMapViewer\CoreKeeperMapViewer.exe
echo Zip:   dist\CoreKeeperMapViewer-windows.zip
exit /b 0
:fail
echo BUILD FAILED
exit /b 1
