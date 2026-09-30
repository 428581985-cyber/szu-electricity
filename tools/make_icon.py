"""生成应用图标 static/favicon.ico（exe 图标 + 网页标签页图标共用）。

纯 Pillow 画的，不依赖任何设计文件。改配色/形状就改下面几个常量，
然后运行：  python tools/make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

# 主色：和网页 dark 主题的强调色一致
TOP = (62, 224, 168)
BOTTOM = (19, 138, 104)
BOLT = (255, 255, 255)

SIZE = 256
SS = 4                 # 先画 4 倍大再缩小，等于手动抗锯齿
RADIUS = 58            # 圆角半径（相对 SIZE）

# 闪电的顶点（0~1 归一化坐标）
BOLT_PTS = [
    (0.63, 0.05), (0.24, 0.55), (0.47, 0.55),
    (0.36, 0.95), (0.78, 0.42), (0.53, 0.42), (0.71, 0.05),
]


def main() -> None:
    n = SIZE * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：竖直渐变，用逐行画线实现（Pillow 没有内置渐变）
    for y in range(n):
        t = y / (n - 1)
        color = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (n, y)], fill=color)

    # 抠成圆角：把圆角外的像素设透明
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=RADIUS * SS, fill=255)
    img.putalpha(mask)

    # 闪电（留出内边距，别顶到边）
    pad = 0.17 * n
    pts = [(pad + x * (n - 2 * pad), pad + y * (n - 2 * pad)) for x, y in BOLT_PTS]
    ImageDraw.Draw(img).polygon(pts, fill=BOLT)

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "static", "favicon.ico")
    img.resize((SIZE, SIZE), Image.LANCZOS).save(
        out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print("wrote", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
