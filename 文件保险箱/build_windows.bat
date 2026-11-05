@echo off
REM For developers only. Colleagues: use the Windows zip (no Python needed), unzip and run the exe.
REM Build the Windows folder version of the file vault app (dist\...\Windows-x64.zip). Requires Python 3.12+.
REM The build script runs --acceptance and --selftest on the packaged app; it fails if they fail.
cd /d "%~dp0"
python --version >nul 2>&1 || (echo [ERROR] Python not found. Install Python 3.12 from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv-build python -m venv .venv-build || goto :err
.venv-build\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt || .venv-build\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt -r requirements-dev.txt || goto :err
.venv-build\Scripts\python scripts\build_app.py || goto :err
echo.
echo Done. See the dist folder.
explorer dist
pause
exit /b 0
:err
echo [ERROR] Build failed. Please send a screenshot of this window.
pause
exit /b 1
