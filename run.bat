@echo off
REM ============================================================
REM  Frontline Hiring Scraper - one-click launcher (Windows)
REM  Double-click this file. First run installs everything
REM  (a few minutes); later runs start in seconds.
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python is not installed. Please install Python 3.11+ from
  echo  https://www.python.org/downloads/  ^(tick "Add Python to PATH"^),
  echo  then double-click this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo Setting up for the first time ^(this takes a few minutes^)...
  python -m venv .venv
  call ".venv\Scripts\activate.bat"
  python -m pip install --upgrade pip >nul
  python -m pip install -r requirements.txt
  python -m playwright install chromium
  echo Creating a "Frontline Hiring Scraper" shortcut on your Desktop...
  powershell -NoProfile -Command ^
    "$s=(New-Object -ComObject WScript.Shell); $lnk=$s.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Frontline Hiring Scraper.lnk'); $lnk.TargetPath='%~f0'; $lnk.WorkingDirectory='%~dp0'; $lnk.IconLocation='%SystemRoot%\System32\SHELL32.dll,13'; $lnk.Save()" 2>nul
) else (
  call ".venv\Scripts\activate.bat"
)

echo.
echo  Starting the Frontline Hiring Scraper...
echo  Your browser will open at http://127.0.0.1:5050
echo  Keep this window open while you use it. Close it to stop.
echo.
start "" "http://127.0.0.1:5050"
python app.py
pause
