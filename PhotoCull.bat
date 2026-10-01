@echo off
rem Double-click to pick a folder, or drag a folder onto this file.
title PhotoCull
"%USERPROFILE%\miniconda3\envs\photoframe\python.exe" "%~dp0photocull.py" %*
if errorlevel 1 pause
