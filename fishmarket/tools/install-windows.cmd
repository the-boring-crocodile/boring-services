@echo off
setlocal
cd /d "%~dp0"
if exist ".venv" (
  echo Existing .venv: use rehearse.cmd or choose a new extraction folder.
  exit /b 1
)
py -3.13 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -I -m pip install --no-index --find-links wheels --require-hashes -r requirements.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -I -m pip check
if errorlevel 1 exit /b 1
echo Installed only in this folder. Next: rehearse.cmd
