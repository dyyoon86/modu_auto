@echo off
cd /d "%~dp0"
echo [1/3] Creating Python venv...
if not exist .venv (
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
echo [2/3] Installing packages...
.venv\Scripts\python.exe -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
echo [3/3] Downloading scrcpy + adb...
if not exist tools mkdir tools
dir /b tools\scrcpy-* >nul 2>nul && goto done
curl -skL -o tools\scrcpy.zip https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-win64-v4.1.zip
powershell -NoProfile -Command "Expand-Archive -Force tools\scrcpy.zip tools; Remove-Item tools\scrcpy.zip"
:done
echo.
echo Setup complete. Start with run.bat
pause
