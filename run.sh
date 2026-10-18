#!/usr/bin/env bash
# 一键启动灵眸（macOS / Linux）。首次运行会自动创建虚拟环境并安装依赖。
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
[ -f config.json ] || cp config.example.json config.json
exec .venv/bin/python app.py
