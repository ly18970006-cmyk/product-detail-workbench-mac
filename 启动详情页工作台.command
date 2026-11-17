#!/bin/zsh
cd -- "${0:A:h}" || exit 1
./.venv/bin/python 启动管理.py start
if [[ $? -ne 0 ]]; then
  read "?启动失败，按回车关闭。"
  exit 1
fi
/usr/bin/open 'http://127.0.0.1:8905/'
