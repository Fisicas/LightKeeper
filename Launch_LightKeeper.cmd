@echo off
setlocal
cd /d "%~dp0"
rem Interpreter: LIGHTKEEPER_PYTHON if set, else the first pythonw.exe on PATH.
set "LK_PY=%LIGHTKEEPER_PYTHON%"
if not defined LK_PY for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined LK_PY set "LK_PY=%%P"
if not exist "%LK_PY%" (
  echo Python not found at %LK_PY%. Set LIGHTKEEPER_PYTHON to a pythonw.exe with PySide6, numpy, scipy, matplotlib, h5py.
  pause
  exit /b 1
)
start "LightKeeper" "%LK_PY%" -X utf8 "%~dp0LightKeeper.py" %*
