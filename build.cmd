@echo off
setlocal
cd /d "%~dp0"
if not exist .venv-build\Scripts\python.exe (
  uv venv --python 3.11 .venv-build || exit /b 1
)
uv pip install --python .venv-build\Scripts\python.exe pyinstaller || exit /b 1

set OMP_EXE=%LOCALAPPDATA%\omp\omp.exe
if exist "%OMP_EXE%" (
  set OMP_ARG=--add-data "%OMP_EXE%;payload"
) else (
  echo ! omp CLI not found at %OMP_EXE% - this build ships without it.
  set OMP_ARG=
)

.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name omd --icon assets\omd.ico --add-data "assets\omd.ico;." omd.pyw || exit /b 1
if exist certs\omd-selfsigned.pfx (
  echo Signing dist\omd.exe ...
  powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1 ^
    -Files dist\omd.exe -Pfx certs\omd-selfsigned.pfx -Password "%OMD_PFX_PASSWORD%" || exit /b 1
)

.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name omd-setup --icon assets\omd.ico %OMP_ARG% ^
  --add-data "dist\omd.exe;payload" --add-data "assets\omd.ico;." installer.py || exit /b 1

if exist certs\omd-selfsigned.pfx (
  echo Signing dist\omd-setup.exe ...
  powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1 ^
    -Files dist\omd-setup.exe -Pfx certs\omd-selfsigned.pfx -Password "%OMD_PFX_PASSWORD%" || exit /b 1
) else (
  echo No certs\omd-selfsigned.pfx - build left unsigned.
)

echo.
echo Built dist\omd-setup.exe
