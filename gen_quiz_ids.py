#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 site/quiz_ids.json —— 挑战组卷用的题目 id 清单（轻量文件）。

为什么需要它：服务端原本每次组卷都要解析 3.1MB 的 content.json，
沙箱冷启动时内存紧张会失败 → 题单为空 → 挑战抽不到题（用户反馈"弹窗说成功但没抽到题"）。
改为读取本文件（7KB）后，读取失败概率极低；即使失败也不再缓存空列表，下次会自动重试。

用法：python gen_quiz_ids.py
"""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(BASE, "site", "content.json")
OUT = os.path.join(BASE, "site", "quiz_ids.json")
VOLUMES = ("高考·综合", "高考·选必")

with open(CONTENT, "r", encoding="utf-8") as f:
    data = json.load(f)

ids = []
for q in (data.get("questions") or []):
    if q.get("volume") not in VOLUMES:
        continue
    try:
        ids.append(int(q.get("id")))
    except (TypeError, ValueError):
        continue

ids = sorted(set(ids))
payload = {"volumes": list(VOLUMES), "count": len(ids), "ids": ids}
with open(OUT, "w", encoding="utf-8", newline="") as f:
    f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

print("[OK] site/quiz_ids.json 已生成：%d 题，%.1f KB" % (len(ids), os.path.getsize(OUT) / 1024))
