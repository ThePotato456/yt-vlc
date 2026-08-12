@echo off
cd /d "%~dp0"

call ".venv\Scripts\activate.bat"

if exist "%~dp0discord_bot.py" (
    python "%~dp0discord_bot.py"
) else (
    echo No discord_bot.py found. Place your Python file in this folder and rename it to discord_bot.py.
    pause
)
