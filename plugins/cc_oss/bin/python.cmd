@echo off
rem Bootstrap a Python 3.10+ runtime on native Windows without a POSIX shell.
rem Probe each candidate before running the workload once; preserve argv and exit.
setlocal
set "PROBE=import sys;raise SystemExit(0 if sys.version_info >= (3,10) else 127)"
python.exe -c "%PROBE%" <nul >nul 2>&1
if not errorlevel 1 goto :python
python3.exe -c "%PROBE%" <nul >nul 2>&1
if not errorlevel 1 goto :python3
for %%V in (3.20 3.19 3.18 3.17 3.16 3.15 3.14 3.13 3.12 3.11 3.10) do (
    py -%%V -c "%PROBE%" <nul >nul 2>&1
    if not errorlevel 1 (
        set "PY_VERSION=%%V"
        goto :py_version
    )
    python%%V.exe -c "%PROBE%" <nul >nul 2>&1
    if not errorlevel 1 (
        set "PY_VERSION=%%V"
        goto :python_version
    )
)
py -3 -c "%PROBE%" <nul >nul 2>&1
if not errorlevel 1 goto :py3
echo python: no Python 3.10+ found on PATH; install one 1>&2
exit /b 127
:python
python.exe %*
exit /b %errorlevel%
:python3
python3.exe %*
exit /b %errorlevel%
:py_version
py -%PY_VERSION% %*
exit /b %errorlevel%
:python_version
python%PY_VERSION%.exe %*
exit /b %errorlevel%
:py3
py -3 %*
exit /b %errorlevel%
