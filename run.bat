@echo off
REM Manual / interactive runner. The scheduled task uses pythonw.exe directly.
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" monitor.py
