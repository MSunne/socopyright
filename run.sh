#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "⚠️  已生成 .env，请修改后重启"
fi

export PYTHONPATH=.
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
