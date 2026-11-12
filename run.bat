@echo off
setlocal
rem TradeVision HUD launcher. First run creates a private venv and installs packages.
rem Packages are reinstalled automatically when requirements.txt changes.
set "APPDIR=%LOCALAPPDATA%\TradeVisionHUD"
set "VENV=%APPDIR%\venv"
set "MARK=%VENV%\requirements.installed.txt"

if not exist "%MARK%" goto setup
fc /b "%~dp0requirements.txt" "%MARK%" >nul 2>&1 || goto install
goto run

:setup
echo [1/2] Creating Python environment (first run only)...
if not exist "%APPDIR%" mkdir "%APPDIR%"
if not exist "%VENV%\Scripts\python.exe" (
  py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
)
if not exist "%VENV%\Scripts\python.exe" goto err

:install
echo [2/2] Installing packages...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "%~dp0requirements.txt"
if errorlevel 1 goto err
copy /y "%~dp0requirements.txt" "%MARK%" >nul

:run
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0main.py" %*
exit /b 0

:err
echo.
echo Setup failed. Check that Python 3.11+ is installed and the internet is connected.
pause
exit /b 1
