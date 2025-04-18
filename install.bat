@echo off
echo 📦 Installiere Abhängigkeiten...

REM Virtuelle Umgebung
python -m venv venv
call venv\Scripts\activate.bat

pip install -r requirements.txt

IF NOT EXIST ".env" (
    echo DISCORD_TOKEN=dein_token_hier > .env
    echo 🔐 Bitte trage deinen Discord-Bot-Token in die .env ein!
) ELSE (
    echo .env-Datei existiert bereits.
)
