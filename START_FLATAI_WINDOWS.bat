@echo off
title FlatAI Docker Launcher (AI-Sana Draft)
echo ===================================================
echo   Starting FlatAI (PostgreSQL + AI NLP & Expert Core)
echo   Team Lead: Baygaskin Madi Dulatovich
echo ===================================================
echo.
echo Launching Docker containers...
start "" "http://localhost:8000"
docker compose up --build
pause
