@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m companion.cli --root "%~dp0." %*
) else (
  python -m companion.cli --root "%~dp0." %*
)
set "exit_code=%ERRORLEVEL%"
popd
exit /b %exit_code%
