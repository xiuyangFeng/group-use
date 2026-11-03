@echo off
REM Run the file vault app directly from source (no exe needed). First run installs dependencies.
REM Requires Python 3.12+ from python.org (tick "Add python.exe to PATH").
cd /d "%~dp0"
python --version >nul 2>&1 || (echo [ERROR] Python not found. Install Python 3.12 from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || .venv\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt || goto :err
)
start "" .venv\Scripts\pythonw run_gui.py
exit /b 0
:err
echo [ERROR] Setup failed. Please send a screenshot of this window.
pause
exit /b 1
