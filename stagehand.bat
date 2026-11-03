@echo off
rem Double-click to open stagehand. Close the Newport CONEX-CC and Kinesis applications first.
cd /d "%~dp0"
uv run stagehand-gui %*
if errorlevel 1 pause
