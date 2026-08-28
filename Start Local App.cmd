@echo off
setlocal
cd /d "%~dp0"
title Make Interactive PDFs

if not exist ".venv\Scripts\python.exe" (
  echo This prototype has not been set up yet.
  echo Follow the one-time setup steps in README.md, then try again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import fastapi, multipart, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo The local app packages are missing.
  echo Follow the one-time setup steps in README.md, then try again.
  pause
  exit /b 1
)

echo Starting the local PDF tool in Chrome...
echo Keep this window open while you use the app.
echo Press Ctrl+C here when you are finished.
echo.
".venv\Scripts\python.exe" -m interactive_pdf_app

if errorlevel 1 (
  echo.
  echo The app stopped with an error. See the message above.
  pause
)
