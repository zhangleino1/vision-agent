@echo off
cd /d %~dp0
call conda activate research
start "" http://127.0.0.1:8765
python app.py
