@echo off
rem Run Keeper from source. Double-click to pick a folder, or drag a folder onto this file.
rem (No Python needed at all? Use dist\Keeper.exe instead - see README "Build the portable exe".)
rem Python is found in this order (see README "Install"):
rem   1. the KEEPER_PYTHON environment variable
rem   2. the "keeper" conda environment
setlocal
title Keeper
set "PY=%KEEPER_PYTHON%"
if not defined PY if exist "%USERPROFILE%\miniconda3\envs\keeper\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\keeper\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\envs\keeper\python.exe" set "PY=%USERPROFILE%\anaconda3\envs\keeper\python.exe"
if not defined PY (
    echo Could not find the "keeper" conda environment.
    echo Create it with:  conda env create -f environment.yml
    pause
    exit /b 1
)
"%PY%" "%~dp0keeper.py" %*
if errorlevel 1 pause
