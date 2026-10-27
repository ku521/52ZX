#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 52ZX 网站生成图标资源，并向 index.html 注入引用。

用法：python build_pwa.py
依赖：Pillow（已装 12.3），仅需标准库。
图标源：site/icon-src.png（即用户原图标 E:\\Desktop\\411.png 的副本）。

两套图标分工（用户明确：只把「保存到主屏」的图标换成紫色盾牌）：
  1) 主屏 App 图标（紫色）：icon-512 / icon-192 / apple-touch-icon(180)
     —— 去白底 + 白色描边 + 淡紫圆角渐变底，随 manifest 用于「添加到主屏幕」。
  2) 站点品牌 logo（蓝色）：logo-brand(512) / favicon-32
     —— 原始蓝标本色（白底蓝盾，不去白底），用于网站 header、登录弹窗、浏览器标签页。

可调：SHORT_NAME（主屏名称）、pad（主屏图标内容边距）、底色 gradient_bg()。
"""
import os
import json
import re
from collections import deque
from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(BASE, "site")
APP_NAME = "高中·文综（政史地生）加油站 · 52ZX吾爱自学"
SHORT_NAME = "52自学"
SRC = os.path.join(SITE, "icon-src.png")

# ---- 1. 读取原图并去白底（边界 flood-fill，只删与画布边缘连通的背景白）----
src = Image.open(SRC).convert("RGBA")
W, H = src.size
px = src.load()

def is_white(c):
    r, g, b, a = c
    return a > 200 and r > 238 and g > 238 and b > 238

bg = bytearray(W * H)
q = deque()
for x in range(W):
    for y in (0, H - 1):
        if is_white(px[x, y]) and bg[y * W + x] == 0:
            bg[y * W + x] = 1
            q.append((x, y))
for y in range(H):
    for x in (0, W - 1):
        if is_white(px[x, y]) and bg[y * W + x] == 0:
            bg[y * W + x] = 1
            q.append((x, y))
while q:
    x, y = q.popleft()
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < W and 0 <= ny < H and bg[ny * W + nx] == 0 and is_white(px[nx, ny]):
            bg[ny * W + nx] = 1
            q.append((nx, ny))
for y in range(H):
    for x in range(W):
        if bg[y * W + x]:
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, 0)
logo = src  # 已是去白底后的 RGBA
print("icon source:", W, "x", H, "background removed -> transparent")

# ---- 2. 渐变底 + 合成 ----
def gradient_bg(size):
    """垂直渐变 #6e43a3 -> #954cb5（原深紫 #4a148c/#7b1fa2 向白色混合 20%，更柔和）。
    避用蓝色：原 logo 本身是蓝橙配色，蓝底会与 logo 撞色；
    紫调与橙色闪电互补对比，且能让白色书页更突出。"""
    top, bot = (0x6e, 0x43, 0xa3), (0x95, 0x4c, 0xb5)
    img = Image.new("RGB", (size, size))
    p = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(size):
            p[x, y] = (r, g, b)
    return img

def compose(size, pad=0.01):
    bg = gradient_bg(size).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    bg.putalpha(mask)
    lw, lh = logo.size
    target = int(size * (1 - 2 * pad))
    scale = target / max(lw, lh)
    nw, nh = max(1, int(lw * scale)), max(1, int(lh * scale))
    l = logo.resize((nw, nh), Image.LANCZOS)
    # 白色描边：膨胀 alpha 通道，避免深蓝盾形在深色底上"沉底"
    k = max(2, int(size * 0.014))
    dilated = l.getchannel("A").filter(ImageFilter.MaxFilter(2 * k + 1))
    stroke = Image.new("RGBA", (nw, nh), (255, 255, 255, 255))
    stroke.putalpha(dilated)
    l = Image.alpha_composite(stroke, l)
    bg.alpha_composite(l, ((size - nw) // 2, (size - nh) // 2))
    return bg

for size, name in [(512, "icon-512.png"), (192, "icon-192.png"), (180, "apple-touch-icon.png")]:
    compose(size).save(os.path.join(SITE, name), "PNG")
    print("wrote", name, size)

# ---- 2b. 蓝色品牌 logo（网站 header / 登录弹窗 / 标签页用，非主屏）----
# 用户原意：只把「保存到主屏」的图标换成紫色盾牌；网站自身的 logo 应保留原始蓝标。
# 原始蓝标 = 白色圆角卡片 + 蓝色盾牌（即 E:\Desktop\411.png 的本色，不去白底）。
# 故此处还原蓝标：白底蓝标做成正方形，由 header 的白卡片 CSS 包裹。
def blue_brand(size):
    raw = Image.open(SRC).convert("RGBA")
    bw, bh = raw.size
    rp = raw.load()
    minx, miny, maxx, maxy = bw, bh, 0, 0
    for y in range(bh):
        for x in range(bw):
            if not is_white(rp[x, y]):
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    crop = raw.crop((minx, miny, maxx + 1, maxy + 1))
    cw, ch = crop.size
    side = max(cw, ch)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.paste(crop, ((side - cw) // 2, (side - ch) // 2), crop)
    if side != size:
        canvas = canvas.resize((size, size), Image.LANCZOS)
    return canvas

# header / 登录弹窗 logo：白底蓝标（正方形），配合白卡片 CSS
blue_brand(512).save(os.path.join(SITE, "logo-brand.png"), "PNG")
print("wrote logo-brand.png (站点 header 蓝标)")
# 标签页 favicon：蓝标（白底，深色/浅色标签均可见）
blue_brand(64).resize((32, 32), Image.LANCZOS).save(os.path.join(SITE, "favicon-32.png"), "PNG")
print("wrote favicon-32.png (蓝标，标签页)")

# ---- 3. manifest ----
manifest = {
    "name": APP_NAME,
    "short_name": SHORT_NAME,
    "description": "高中政史地生（文综）学习加油站",
    "lang": "zh-CN",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#eef1f7",
    "theme_color": "#3b6fe0",
    "icons": [
        {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
    ],
}
for p in [os.path.join(SITE, "manifest.webmanifest"), os.path.join(BASE, "manifest.webmanifest")]:
    with open(p, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
print("wrote manifest.webmanifest (site/ + root)")

# ---- 4. 向 index.html 注入引用 ----
inject = (
    '<link rel="manifest" href="/manifest.webmanifest">\n'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
    '<meta name="apple-mobile-web-app-title" content="%s">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
    '<meta name="theme-color" content="#3b6fe0">\n' % SHORT_NAME
)
anchor = '<meta name="apple-mobile-web-app-capable" content="yes">'
for p in [os.path.join(SITE, "index.html"), os.path.join(BASE, "index.html")]:
    s = open(p, encoding="utf-8").read()
    if 'rel="manifest"' in s:
        print("skip (already injected):", p)
        continue
    assert anchor in s, "anchor not found: " + p
    s = s.replace(anchor, anchor + "\n" + inject, 1)
    open(p, "w", encoding="utf-8").write(s)
    print("injected:", p)
print("DONE")
