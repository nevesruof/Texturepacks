@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Ejecuta instalar.bat primero.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m texturebot run
pause
