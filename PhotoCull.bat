@echo off
rem Run PhotoCull from source. Double-click to pick a folder, or drag a folder onto this file.
rem (No Python needed at all? Use dist\PhotoCull.exe instead - see README "Build the portable exe".)
rem Python is found in this order (see README "Install"):
rem   1. the PHOTOCULL_PYTHON environment variable
rem   2. the "photocull" conda environment
setlocal
title PhotoCull
set "PY=%PHOTOCULL_PYTHON%"
if not defined PY if exist "%USERPROFILE%\miniconda3\envs\photocull\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\photocull\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\envs\photocull\python.exe" set "PY=%USERPROFILE%\anaconda3\envs\photocull\python.exe"
if not defined PY (
    echo Could not find the "photocull" conda environment.
    echo Create it with:  conda env create -f environment.yml
    pause
    exit /b 1
)
"%PY%" "%~dp0photocull.py" %*
if errorlevel 1 pause
