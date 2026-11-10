@echo off
rem Builds EOS5D_ShutterCount.exe (one file, Python and libusb inside, asks for admin rights).
rem Build-time needs: py -m pip install pyinstaller pyusb libusb-package
cd /d "%~dp0"
py -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
  --name EOS5D_ShutterCount ^
  --collect-binaries libusb_package ^
  --hidden-import usb.backend.libusb1 ^
  shutter_count_app.py
if errorlevel 1 exit /b 1
echo.
echo Built: %~dp0dist\EOS5D_ShutterCount.exe
