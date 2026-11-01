@echo off
REM Build CT_Deid.exe on Windows. Requires Python 3.10+ from python.org (tick "Add python.exe to PATH").
cd /d "%~dp0"
python --version >nul 2>&1 || (echo [ERROR] Python not found. Install Python 3.12 from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv-build python -m venv .venv-build || goto :err
call .venv-build\Scripts\activate.bat
python -m pip install -r requirements.txt pyinstaller || python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --onefile --windowed --name CT_Deid app.py || goto :err
echo.
echo Done: %~dp0dist\CT_Deid.exe
explorer dist
pause
exit /b 0
:err
echo [ERROR] Build failed. Please send a screenshot of this window.
pause
exit /b 1
