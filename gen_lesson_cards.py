# -*- coding: utf-8 -*-
"""按课本课次生成知识点背诵卡（润色版）：每课1张，front=课次，back=核心考点。
复用 kb_clean 的高考级清洗，优先定义句/本质核心句/意义作用句，彻底去除栏目噪声与断头碎片。
"""
import json, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from kb_clean import clean_lesson_text, pick_def, pick_mark

SUBJ = {'思想政治': '政治', '历史': '历史', '地理': '地理', '生物学': '生物'}

d = json.load(open('site/content.json', encoding='utf-8'))
idx = json.load(open('books/textbook_index.json', encoding='utf-8'))
lt = json.load(open('books/lesson_text.json', encoding='utf-8'))

# 纲要下关键词包（与 remap 保持一致）
src = open('remap_textbook_units.py', encoding='utf-8').read()
KWBAG = {}
m = re.search(r'KWBAG = \{(.*?)\n\}', src, re.S)
if m:
    for km in re.finditer(r"'(第\d+课[^']*)': '([^']*)'", m.group(1)):
        KWBAG[km.group(1)] = km.group(2)

NUMS = '①②③④⑤⑥⑦⑧'


def trim_point(s, maxn=78):
    """长点按句/逗号边界裁剪为单一事实，避免 PDF 无句号导致的两句粘连。"""
    if len(s) <= maxn:
        return s
    cut = -1
    for i, ch in enumerate(s):
        if i > maxn:
            break
        if ch in '。，':
            cut = i
    return s[:cut] if cut > 12 else s[:maxn]


def extract_points(lesson, body):
    sents = clean_lesson_text(body, lesson)
    pts = []
    seen = set()
    for s in pick_def(sents, 3) + pick_mark(sents, 5):
        if s not in seen:
            seen.add(s)
            pts.append(trim_point(s))
        if len(pts) >= 5:
            break
    return pts


def unit_of(bk, lesson):
    book = bk.split('|')[1].replace('选择性必修', '选必')
    return f'{book}·{lesson}'


added = 0
# 重跑安全：先移除旧课次卡
d['cards'] = [c for c in d['cards'] if c.get('tag') != '课本同步']
exist_fronts = {c.get('front') for c in d['cards']}
for bk, meta in idx.items():
    subj_cn = SUBJ[bk.split('|')[0]]
    bookname = bk.split('|')[1]
    if '选必' in bookname and bk.split('|')[0] != '地理':
        vol = '高考·选必' if bk.split('|')[0] in ('思想政治', '历史') else '高考·综合'
    elif '选必' in bookname:
        vol = '高考·综合'  # 地理选必2并入综合
    else:
        vol = '高考·综合'
    texts = lt.get(bk, {})
    for lesson in meta['lessons']:
        unit = unit_of(bk, lesson)
        front = f'{lesson}（{bookname.replace("选择性必修", "选必")}）'
        if front in exist_fronts:
            continue
        body = texts.get(lesson, '')
        pts = extract_points(lesson, body) if body else []
        if pts:
            back = '【核心考点】' + '<br>'.join(f'{NUMS[i]} {p}' for i, p in enumerate(pts))
        else:
            kw = KWBAG.get(lesson, '')
            back = '【核心考点】' + (kw.replace(' ', ' / ') if kw else '结合课本研读本课主干知识，配套题目见题库同课次。')
        d['cards'].append({
            'subject': subj_cn, 'volume': vol, 'unit': unit, 'tag': '课本同步',
            'front': front, 'back': back, 'star': False})
        exist_fronts.add(front)
        added += 1

json.dump(d, open('site/content.json', 'w', encoding='utf-8'),
          ensure_ascii=False, separators=(',', ':'))
print(f'润色后课本课次卡 {added} 张，总卡数 {len(d["cards"])}')
from collections import Counter
c = Counter((x['subject'], x['volume']) for x in d['cards'] if x.get('tag') == '课本同步')
for k, n in sorted(c.items()):
    print(' ', k, n)
