@echo off
REM Run the tool directly from source (no exe needed). First run installs dependencies.
cd /d "%~dp0"
python --version >nul 2>&1 || (echo [ERROR] Python not found. Install Python 3.12 from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv-run (
  python -m venv .venv-run || goto :err
  .venv-run\Scripts\python -m pip install -r requirements.txt || .venv-run\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt || goto :err
)
.venv-run\Scripts\python app.py
exit /b 0
:err
echo [ERROR] Setup failed. Please send a screenshot of this window.
pause
exit /b 1
