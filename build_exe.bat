@echo off
REM Build OpenGameCredit.exe (single-file, windowed, no console)
REM Requires: pyinstaller requests cryptography pillow
setlocal
chcp 65001 >nul

set "PY="
set "PYDESC="

REM 1) Python launcher: pick the first version that already has PyInstaller
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if not defined PY (
        py -%%V -c "import PyInstaller" >nul 2>&1 && (
            set "PY=py -%%V"
            set "PYDESC=py -%%V"
        )
    )
)

REM 2) Common install directories (covers interpreters the launcher does not list)
if not defined PY (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python*\python.exe"
        "C:\Python*\python.exe"
        "D:\Python*\python.exe"
    ) do (
        if not defined PY (
            "%%~fD" -c "import PyInstaller" >nul 2>&1 && (
                set PY="%%~fD"
                set "PYDESC=%%~fD"
            )
        )
    )
)

REM 3) Fall back to whatever "python" resolves to
if not defined PY (
    python -c "import PyInstaller" >nul 2>&1 && (
        set "PY=python"
        set "PYDESC=python"
    )
)

if not defined PY (
    echo [ERROR] No Python interpreter with PyInstaller found.
    echo Install it first, for example:
    echo     py -3.13 -m pip install pyinstaller requests cryptography pillow
    pause
    exit /b 1
)

echo [INFO] Interpreter: %PYDESC%
%PY% -m PyInstaller --version

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name OpenGameCredit ^
  --hidden-import cryptography ^
  --hidden-import PIL._tkinter_finder ^
  app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo [OK] Output: dist\OpenGameCredit.exe
pause
exit /b 0
