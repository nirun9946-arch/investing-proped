@echo off
chcp 65001 >nul
title Investing Pro Dashboard
cd /d "%~dp0"
python app.py
pause
