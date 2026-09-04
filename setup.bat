@echo off
rem Double-click setup for mailbrief. Uses your system Python.
cd /d "%~dp0"
echo Running mailbrief setup...
echo.
python setup.py
if errorlevel 1 (
  echo.
  echo Setup did not finish. Read the message above, fix what it names, then run setup.bat again.
)
pause
