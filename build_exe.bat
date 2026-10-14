@echo off
REM 打包 OpenGameCredit.exe（Windows 单文件、无控制台窗口）
REM 依赖: pip install pyinstaller requests cryptography pillow
chcp 65001 >nul

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name OpenGameCredit ^
  --hidden-import cryptography ^
  --hidden-import PIL._tkinter_finder ^
  app.py

echo.
echo 生成结果: dist\OpenGameCredit.exe
pause
