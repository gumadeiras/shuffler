@echo off
rem Double-click to open shuffler. Close the Newport CONEX-CC and Kinesis applications first.
cd /d "%~dp0"
uv run shuffler-gui %*
if errorlevel 1 pause
