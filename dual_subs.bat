@echo off
:: Double-click launcher for the Python version (no .exe build needed).
:: Drop this next to dual_subs.py in the folder you want to process, or pass
:: folders/files as arguments.
setlocal
set "HERE=%~dp0"
where py >nul 2>&1 && (py -3 "%HERE%dual_subs.py" %* & goto :done)
where python >nul 2>&1 && (python "%HERE%dual_subs.py" %* & goto :done)
echo Python 3 was not found. Install it from https://python.org
pause
:done
endlocal
