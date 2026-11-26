#!/bin/zsh
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 创建 .venv 并安装依赖。'
  read -r '?按回车退出'
  exit 1
fi
if ! .venv/bin/python -c 'import pypdf, send2trash, PIL' 2>/dev/null; then
  echo '依赖不完整，请运行 .venv/bin/python -m pip install -r requirements.txt'
  read -r '?按回车退出'
  exit 1
fi
.venv/bin/python app.py
