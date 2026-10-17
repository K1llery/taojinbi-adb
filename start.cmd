@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
  start "" pythonw -m taojinbi
  exit /b
)
where python >nul 2>nul
if %errorlevel% equ 0 (
  python -m taojinbi
  if errorlevel 1 pause
  exit /b
)
echo Python 3.14 with Tkinter is required. See README.md.
pause
