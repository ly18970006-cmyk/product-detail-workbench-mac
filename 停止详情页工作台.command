#!/bin/zsh
cd -- "${0:A:h}" || exit 1
./.venv/bin/python 启动管理.py stop
if [[ $? -ne 0 ]]; then
  read "?停止未完成，按回车关闭。"
  exit 1
fi
