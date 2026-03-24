@echo off
chcp 65001 >nul
echo ============================================
echo   MusicBot - Installation
echo ============================================
echo.

REM --- Python pruefen ---
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [FEHLER] Python nicht gefunden. Bitte Python 3.10+ installieren:
    echo   https://www.python.org/downloads/
    pause
    exit /b 1
)

REM --- FFmpeg pruefen und ggf. installieren ---
ffmpeg -version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [INFO] FFmpeg nicht gefunden. Installiere via winget...
    winget install --id Gyan.FFmpeg -e --silent
    IF ERRORLEVEL 1 (
        echo [FEHLER] FFmpeg-Installation fehlgeschlagen.
        echo   Bitte manuell installieren: https://ffmpeg.org/download.html
        echo   FFmpeg muss im PATH verfuegbar sein.
        pause
        exit /b 1
    )
    echo [OK] FFmpeg installiert. Bitte ein neues Terminal oeffnen, damit PATH aktualisiert wird.
) ELSE (
    echo [OK] FFmpeg gefunden.
)

REM --- Virtuelle Umgebung ---
IF NOT EXIST "venv" (
    echo [INFO] Erstelle virtuelle Umgebung...
    python -m venv venv
)
call venv\Scripts\activate.bat

REM --- Abhaengigkeiten ---
echo [INFO] Installiere Python-Abhaengigkeiten...
pip install --upgrade pip >nul
pip install --upgrade -r requirements.txt

REM --- .env anlegen ---
IF NOT EXIST ".env" (
    echo DISCORD_TOKEN=dein_token_hier > .env
    echo.
    echo [WICHTIG] .env wurde erstellt.
    echo   Trage deinen Discord-Bot-Token ein: .env
) ELSE (
    echo [OK] .env existiert bereits.
)

echo.
echo ============================================
echo   Installation abgeschlossen.
echo   Bot starten mit: start.bat
echo ============================================
pause
