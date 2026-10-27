# -*- coding: utf-8 -*-
"""harvest_papers.py  v2
解析 .workbuddy/work/extracted/ 真实高考/模拟卷 txt，提取选择题并配对答案。
用法:
  python harvest_papers.py            # 仅报告, 写出 harvest_candidates.json
  python harvest_papers.py --inject   # 报告 + 注入 content.json
"""
import os, re, json, glob, sys, hashlib

EXTRACT_DIR = r"C:/Users/25205/Downloads/政史地资料/.workbuddy/work/extracted"
CONTENT = r"C:/Users/25205/Downloads/政史地资料/52ZX/site/content.json"
OUT = r"C:/Users/25205/Downloads/政史地资料/52ZX/harvest_candidates.json"

SUBJ_MAP = [("历史", "历史"), ("政治", "政治"), ("地理", "地理"), ("生物", "生物")]

def detect_subject(name):
    for k, v in SUBJ_MAP:
        if k in name:
            return v
    return None

Q_START = re.compile(r"^\s*(\d{1,3})\s*[．.、]\s*(.*)$")
OPT_MARK = re.compile(r"([A-D])\s*[．.、]")
ANS_LINE = re.compile(r"^\s*(\d{1,3})\s*[．.、]?\s*([A-D])\s*$", re.M)
SECTION = re.compile(r"^[一二三四五六七八九十]+[、.．]")
END_Q = re.compile(r"^\s*(【答案】|【解析】|【详解】|解析|详解|考点|答案[：:]|——参考答案)")

def is_section_header(line):
    s = line.strip()
    if SECTION.match(s):
        return True
    if "参考答案" in s and len(s) < 12:
        return True
    return False

def is_answer_file(name):
    b = os.path.basename(name)
    return ("【答案】" in b) or (("答案" in b) and ("试题" not in b) and ("试卷" not in b))

def norm_key(name):
    base = os.path.basename(name)
    base = re.sub(r"【答案】|答案|（答案）|\(答案\)", "", base)
    return base.replace(" ", "")

def short_paper(name):
    base = os.path.basename(name)
    # 取最后一个 __ 段作为试卷名
    if "__" in base:
        base = base.rsplit("__", 1)[-1]
    base = re.sub(r"【答案】", "", base)
    # 去除【...】括号内噪声（期次/周次等）
    base = re.sub(r"【[^】]*】", "", base)
    base = re.sub(r"\.(txt|docx|pdf)$", "", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base or "真题模拟"

def parse_answers_from_text(text):
    ans = {}
    lines = text.splitlines()
    # 1) 表格 题号|..  答案|..
    for i, l in enumerate(lines):
        if "题号" in l and "|" in l:
            nums = [x.strip() for x in l.split("|") if x.strip() and x.strip() != "题号"]
            for j in range(i + 1, min(i + 4, len(lines))):
                if "答案" in lines[j] and "|" in lines[j]:
                    vals = [x.strip() for x in lines[j].split("|") if x.strip() and x.strip() != "答案"]
                    for num, val in zip(nums, vals):
                        if re.match(r"^\d+$", num) and re.match(r"^[A-D]$", val):
                            ans[int(num)] = val
                    break
    # 2) 逐题 N．X / N.X
    for m in ANS_LINE.finditer(text):
        ans[int(m.group(1))] = m.group(2)
    # 3) 参考答案块兜底
    idx = -1
    for kw in ("参考答案", "参考答案及评分标准", "答案及解析"):
        p = text.find(kw)
        if p != -1:
            idx = max(idx, p)
    if idx != -1:
        for m in ANS_LINE.finditer(text[idx:]):
            ans[int(m.group(1))] = m.group(2)
    # 4) 题组连排答案: 【答案】1．A 2．C 3．C / 答案 1.A 2.B
    for m in re.finditer(r"【?答案】?\s*([\s\S]*?)(?=【解析】|【详解】|【点睛】|【答案】|$)", text):
        seg = m.group(1)
        for mm in re.finditer(r"(\d{1,3})\s*[．.、]\s*([A-D])", seg):
            ans[int(mm.group(1))] = mm.group(2)
    return ans

def parse_questions(text):
    lines = text.splitlines()
    blocks = []
    cur = None
    for line in lines:
        m = Q_START.match(line)
        if m and not is_section_header(line):
            if cur:
                blocks.append(cur)
            cur = (int(m.group(1)), m.group(2))
        elif cur is not None:
            if END_Q.match(line.strip()):
                blocks.append(cur)
                cur = None
            else:
                cur = (cur[0], cur[1] + "\n" + line)
    if cur:
        blocks.append(cur)
    qs = []
    for qnum, blk in blocks:
        stem, opts = extract_q(blk)
        if stem and len(opts) >= 2:
            qs.append((qnum, stem, opts))
    return qs

def extract_q(blk):
    marks = [(m.start(), m.group(1)) for m in OPT_MARK.finditer(blk)]
    # 取第一个 A 作为选项区起点
    start_idx = next((i for i, (_, L) in enumerate(marks) if L == "A"), None)
    if start_idx is None:
        # 退而求其次：取第一个出现的选项字母
        if not marks:
            return blk.strip(), {}
        start_idx = 0
    first_pos = marks[start_idx][0]
    stem = blk[:first_pos].strip()
    # 去掉 stem 内部残留的选项标记污染（不太可能，因为 firstA 之前无 A-D 有序标记）
    opts = {}
    expected = "A"
    i = start_idx
    while i < len(marks):
        pos, L = marks[i]
        if L == expected:
            nxt = marks[i + 1][0] if i + 1 < len(marks) else len(blk)
            txt = blk[pos + 1:nxt]
            txt = re.sub(r"^\s*[．.、]\s*", "", txt).strip()
            opts[L] = txt
            expected = chr(ord(expected) + 1)
            i += 1
        else:
            i += 1
        if expected > "D":
            break
    return stem, opts

def main():
    files = glob.glob(os.path.join(EXTRACT_DIR, "**", "*.txt"), recursive=True)
    answer_by_key = {}
    answers_by_path = {}
    for f in files:
        subj = detect_subject(f)
        if not subj:
            continue
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        ans = parse_answers_from_text(txt)
        if ans:
            answers_by_path[f] = ans
            if is_answer_file(f):
                answer_by_key.setdefault(norm_key(f), {}).update(ans)

    candidates = []
    stat = {}
    seen = {}
    for f in files:
        subj = detect_subject(f)
        if not subj or is_answer_file(f):
            continue
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        qs = parse_questions(txt)
        if not qs:
            continue
        ans = answers_by_path.get(f, {})
        if not ans:
            ans = answer_by_key.get(norm_key(f), {})
        if not ans:
            continue
        paper = short_paper(f)
        cnt = 0
        for qnum, stem, opts in qs:
            if len(opts) < 2:
                continue
            letter = ans.get(qnum)
            if not letter or letter not in opts:
                continue
            key = hashlib.md5((subj + stem[:50]).encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen[key] = True
            # 清理 stem 里的冗余题号前缀与空格
            stem = re.sub(r"^\s*\d{1,3}\s*[．.、]\s*", "", stem)
            stem = re.sub(r"\s+", " ", stem).strip()
            stem = stem.replace("\n", "<br>")
            opts = {k: re.sub(r"\s+", " ", v).strip() for k, v in opts.items()}
            candidates.append({
                "subject": subj, "unit": paper, "stem": stem[:600],
                "options": opts, "answer": letter, "source": os.path.basename(f),
            })
            cnt += 1
        if cnt:
            stat[subj] = stat.get(subj, 0) + cnt
    json.dump(candidates, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("抽取候选题（带答案+去重）:", len(candidates), "按科:", stat)
    print("写出:", OUT)

    if "--inject" in sys.argv:
        inject(candidates)
    return candidates

def inject(candidates):
    d = json.load(open(CONTENT, encoding="utf-8"))
    existing = set()
    for q in d["questions"]:
        existing.add(hashlib.md5((q.get("subject", "") + (q.get("stem", "") or "")[:50]).encode("utf-8")).hexdigest())
    maxid = max((int(q.get("id", 0)) for q in d["questions"]), default=0)
    vol = "高考·综合"
    added = 0
    for c in candidates:
        key = hashlib.md5((c["subject"] + c["stem"][:50]).encode("utf-8")).hexdigest()
        if key in existing:
            continue
        existing.add(key)
        maxid += 1
        d["questions"].append({
            "no": maxid, "subject": c["subject"], "unit": c["unit"], "stem": c["stem"],
            "options": c["options"], "answer": c["answer"], "expl": "",
            "source": c["source"], "id": maxid, "volume": vol, "type": "选择",
            "paper_tag": "高考综合",
        })
        added += 1
    json.dump(d, open(CONTENT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"已注入 {added} 题，题库现总数 {len(d['questions'])}")

if __name__ == "__main__":
    main()
