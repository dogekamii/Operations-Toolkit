@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --hidden-import keyring.backends.Windows --name "Operations-Toolkit-v1.6.0" cnmaestro_speed_manager.py
pause
