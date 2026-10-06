@echo off
cd /d "%~dp0"
py -3 -c "import sys; assert sys.version_info >= (3,11), 'Necesitas Python 3.11 o superior'"
if errorlevel 1 goto error
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error
if not exist .env copy .env.example .env >nul
echo.
echo Instalado. Completa .env y luego abre iniciar.bat.
pause
exit /b 0
:error
echo Revisa el error anterior. Instala Python 3.11 o superior desde python.org.
pause
exit /b 1
