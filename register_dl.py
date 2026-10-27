# -*- coding: utf-8 -*-
"""register_dl.py  把真实试卷 + 模拟卷 注册进 server.py 的 DL_FILES"""
import json, os

SERVER = r"C:/Users/25205/Downloads/政史地资料/52ZX/server.py"
REAL = r"C:/Users/25205/Downloads/政史地资料/52ZX/dl_real_copied.json"

real = json.load(open(REAL, encoding="utf-8"))
entries = []

# 模拟卷 (地理/生物)
mock_meta = {
    "地理": ("geo_mock", "地理高考模拟卷（%s）", "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"),
    "生物": ("bio_mock", "生物高考模拟卷（%s）", "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"),
}
for subj, (pre, name_tpl, desc) in mock_meta.items():
    files = sorted(f for f in os.listdir(r"C:/Users/25205/Downloads/政史地资料/52ZX/site/files/dl")
                   if f.startswith(f"mock_{subj}_") and f.endswith(".docx"))
    for idx, f in enumerate(files, 1):
        entries.append({
            "id": f"{pre}{idx}", "file": f,
            "name": name_tpl % (["一", "二", "三", "四", "五", "六"][idx - 1]),
            "subject": subj, "desc": desc,
        })

# 真实试卷
for i, r in enumerate(real, 1):
    sid = "hist_r" if r["subject"] == "历史" else "pol_r"
    entries.append({
        "id": f"{sid}{i}", "file": r["file"], "name": r["name"],
        "subject": r["subject"], "desc": r["desc"],
    })

# 生成 Python 文本
def fmt(e):
            return ("    {\"id\": \"%s\", \"file\": \"%s\", \"name\": \"%s\", \"subject\": \"%s\",\n"
                    "     \"desc\": \"%s\"},\n") % (e["id"], e["file"], e["name"], e["subject"], e["desc"])

new_block = "".join(fmt(e) for e in entries)

text = open(SERVER, encoding="utf-8").read()
anchor = '    # 后续把“测试试卷原文档”放进来即可：在 _dl_dir() 目录放入 docx，并在此处加一行（desc 写清楚内容）。\n]'
assert anchor in text, "找不到 DL_FILES 锚点"
text = text.replace(anchor, new_block + "    # 后续把“测试试卷原文档”放进来即可：在 _dl_dir() 目录放入 docx，并在此处加一行（desc 写清楚内容）。\n]", 1)
open(SERVER, "w", encoding="utf-8").write(text)
print("已注册新条目:", len(entries))
for e in entries[:3]:
    print("  ", e["id"], e["file"])
print("  ... 共", len(entries), "条")
