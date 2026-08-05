@echo off
chcp 65001 >nul
title Investing Pro
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_launch.ps1"
