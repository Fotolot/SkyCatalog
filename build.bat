@echo off
REM Buduje dist\SkyCatalog_1.01\SkyCatalog_1.01.exe. Uruchom z folderu projektu: build.bat
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Tworzenie srodowiska wirtualnego .venv ...
    py -3 -m venv .venv || python -m venv .venv || goto :error
)

echo [2/4] Instalowanie PyInstaller ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :error

echo [3/4] Uruchamianie testow ...
".venv\Scripts\python.exe" -m unittest discover -s tests -t . || goto :error

echo [4/4] Budowanie pliku .exe ...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm sortowanie.spec || goto :error

echo.
echo Gotowe: %cd%\dist\SkyCatalog_1.01\SkyCatalog_1.01.exe
exit /b 0

:error
echo.
echo BLAD: budowanie nie powiodlo sie.
exit /b 1
