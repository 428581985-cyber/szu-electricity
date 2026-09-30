"""目录约定：谁写数据、谁读资源。

- **直接跑源码**（`python app.py`）：资源和数据都在项目目录里，最简单。
- **跑打包好的 exe**（PyInstaller，`sys.frozen` 为真）：
  · `static/` 这类只读资源被打进了 exe，运行时解包到 `sys._MEIPASS`（临时目录，退出即删）；
  · 而 `config.json` / `data/` 是**用户数据**，必须落在 **exe 旁边** —— 写到解包目录会丢。

所有模块统一从这里取路径，别再各自 `dirname(__file__)`（打包后那是临时目录）。
"""
from __future__ import annotations

import os
import sys

FROZEN = getattr(sys, "frozen", False)

# 用户数据（config.json / data/）落地的目录
BASE = (
    os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
    else os.path.dirname(os.path.abspath(__file__))
)

# 只读资源（static/）所在的目录：打包后是临时解包目录
RES_DIR = getattr(sys, "_MEIPASS", BASE) if FROZEN else BASE
