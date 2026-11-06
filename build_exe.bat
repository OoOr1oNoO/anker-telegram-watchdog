@echo off
rem Builds dist\anker_ups.exe on Windows (without charts: the PC instance normally runs without Telegram). Needs uv (no Git): https://docs.astral.sh/uv/
cd /d "%~dp0"
uv run --with pyinstaller --with https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz ^
  pyinstaller --onefile --noconsole --name anker_ups --collect-submodules anker_solix_api --collect-submodules ankerups ^
  --exclude-module matplotlib --exclude-module numpy --exclude-module PIL anker_ups.py
if errorlevel 1 exit /b 1
if exist .env copy /y .env dist\.env >nul
echo Done: dist\anker_ups.exe
