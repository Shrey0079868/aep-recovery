@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

:banner
echo ================================================
echo AEP Recovery Lab - Forensic Recovery First
echo ================================================
echo.

REM Windows Python launcher is preferred. It works when python.exe is not on PATH.
where py.exe >nul 2>&1
if not errorlevel 1 goto use_py

REM Fall back to python.exe.
where python.exe >nul 2>&1
if not errorlevel 1 goto use_python

REM Last fallback.
where python3.exe >nul 2>&1
if not errorlevel 1 goto use_python3

goto no_python

:use_py
echo Python detected via Windows Python Launcher (py).
py -3 --version
if errorlevel 1 goto try_python

echo Starting recovery engine with: py -3
echo.
py -3 app.py
goto finish

:try_python
where python.exe >nul 2>&1
if not errorlevel 1 goto use_python
where python3.exe >nul 2>&1
if not errorlevel 1 goto use_python3
goto no_python

:use_python
echo Python detected via python.exe.
python.exe --version
if errorlevel 1 goto no_python
echo Starting recovery engine with: python.exe
echo.
python.exe app.py
goto finish

:use_python3
echo Python detected via python3.exe.
python3.exe --version
if errorlevel 1 goto no_python
echo Starting recovery engine with: python3.exe
echo.
python3.exe app.py
goto finish

:no_python
echo.
echo ERROR: No usable Python installation was found.
echo.
echo The launcher could not start Python from this environment.
echo Try opening Command Prompt and running: py --version
echo.
goto finish

:finish
echo.
echo Recovery Lab has finished.
pause
endlocal
