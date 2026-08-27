@echo off
cd /d "%~dp0"
title Paper Research Agent

set "PY="

py -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py"

if not defined PY (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  echo.
  echo   Python not found.
  echo.
  echo   Install Python from: https://www.python.org/downloads/
  echo   IMPORTANT: check "Add python.exe to PATH" on the first screen.
  echo.
  pause
  exit /b 1
)

%PY% launcher.py
set "CODE=%errorlevel%"
if not "%CODE%"=="0" pause
exit /b %CODE%
