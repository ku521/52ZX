# -*- coding: utf-8 -*-
"""pack_real_papers.py
精选本地真实 历史/政治 高考+模拟卷 docx -> site/files/dl/ , 命名规范化。
优先带【答案/解析】版本。输出 copied.json 供注册 DL_FILES 使用。
"""
import os, json, shutil, re

SRC_ROOT = r"C:/Users/25205/Downloads/政史地资料/政史地资料"
DL = r"C:/Users/25205/Downloads/政史地资料/52ZX/site/files/dl"
OUT_JSON = r"C:/Users/25205/Downloads/政史地资料/52ZX/dl_real_copied.json"

PRIORITY = [("高考", 0), ("真题", 0), ("零模", 1), ("一模", 1), ("二模", 1), ("三模", 1),
            ("四模", 1), ("质检", 1), ("诊断", 1), ("联考", 1), ("模拟", 2), ("期末", 2), ("月考", 2)]

def clean_name(base):
    b = re.sub(r"\.docx$", "", base)
    # 去掉【答案】/解析版等标记
    b = re.sub(r"【答案】|答案|（答案）|解析版|原卷版|教师用卷", "", b)
    # 去掉所有 【...】 括号内期次/来源噪声
    b = re.sub(r"【[^】]*】", "", b)
    # 去掉重复序号 (1)(2)
    b = re.sub(r"\(\d+\)", "", b)
    # 取最后一个 】 之后的含义段（若无 】 则保留整体）
    if "】" in b:
        b = b.split("】")[-1]
    # 开头连续年份只保留一个（如 "20252025陕晋宁青" -> "2025陕晋宁青"）
    b = re.sub(r"^(20\d{2})(?:\s*20\d{2})*", r"\1", b)
    b = b.replace(" ", "").strip()
    return b

def priority_of(name):
    for kw, p in PRIORITY:
        if kw in name:
            return p
    return 9

def collect(subj):
    root = os.path.join(SRC_ROOT, subj + "资料")
    papers = {}  # key -> {'q':path,'a':path}
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if not fn.lower().endswith(".docx"):
                continue
            if fn.startswith("~$"):
                continue
            base = fn
            is_ans = ("【答案】" in base) or ("答案" in base and ("解析" in base))
            key = clean_name(base)
            if not key:
                continue
            # 仅收录含优先关键词的
            if priority_of(key) == 9 and priority_of(base) == 9:
                continue
            k = clean_name(base)
            if k not in papers:
                papers[k] = {"q": None, "a": None}
            if is_ans:
                papers[k]["a"] = os.path.join(dp, fn)
            else:
                papers[k]["q"] = os.path.join(dp, fn)
    # 评分排序：优先带解析，再按 priority + 年份倒序
    items = []
    for k, v in papers.items():
        path = v["a"] or v["q"]
        if not path:
            continue
        yr = re.search(r"(20\d{2})", k)
        year = int(yr.group(1)) if yr else 0
        score = (1 if v["a"] else 0, -priority_of(k), -year)
        items.append((score, k, path, bool(v["a"])))
    items.sort(key=lambda x: x[0], reverse=True)
    return items

def main():
    copied = []
    for subj in ("历史", "政治"):
        items = collect(subj)[:12]
        seen = set()
        for i, (sc, k, path, has_ans) in enumerate(items, 1):
            if k in seen:
                continue
            seen.add(k)
            newname = f"{subj}-{k}{'（解析）' if has_ans else ''}.docx"
            newpath = os.path.join(DL, newname)
            # 去重名
            if os.path.exists(newpath):
                newname = f"{subj}-{k}_{i}{'（解析）' if has_ans else ''}.docx"
                newpath = os.path.join(DL, newname)
            shutil.copy2(path, newpath)
            copied.append({"subject": subj, "file": newname, "name": f"{k}{'（解析版）' if has_ans else ''}",
                           "desc": f"{subj}真实试卷（{ '含详细解析' if has_ans else '原卷' }），来源：智慧中小学/各地模拟与高考真题整理。",
                           "size": os.path.getsize(newpath)})
            print(f"  {subj} 复制 {newname}  ({os.path.getsize(newpath)//1024}KB)")
    json.dump(copied, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("共复制:", len(copied), "写出:", OUT_JSON)

if __name__ == "__main__":
    main()
