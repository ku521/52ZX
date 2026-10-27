# -*- coding: utf-8 -*-
"""阶段2：从课本正文生成每课「同步学习」详细知识页。

产出：d['subjects'][科目][卷].units[unit] = { title, summary(HTML) }
- 课文导学 / 核心概念 / 重难点突破 / 易错警示 / 知识结构
- 正文为空的课用 KWBAG 关键词包兜底；均无则引用课本同步卡
"""
import json, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from kb_clean import clean_lesson_text, pick_def, pick_mark, pick_warn, intro_of

d = json.load(open('site/content.json', encoding='utf-8'))
idx = json.load(open('books/textbook_index.json', encoding='utf-8'))
lt = json.load(open('books/lesson_text.json', encoding='utf-8'))

SUBJ = {'思想政治': '政治', '历史': '历史', '地理': '地理', '生物学': '生物'}

# ---- 关键词包（与 remap 一致：纲要下无正文） ----
src = open('remap_textbook_units.py', encoding='utf-8').read()
KWBAG = {}
m = re.search(r'KWBAG = \{(.*?)\n\}', src, re.S)
if m:
    for km in re.finditer(r"'(第\d+课[^']*)': '([^']*)'", m.group(1)):
        KWBAG[km.group(1)] = km.group(2)


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def build_page(bookname, lesson, body):
    """返回 (title, summary_html)"""
    title = f'{lesson}（{bookname.replace("选择性必修", "选必")}）'
    sents = clean_lesson_text(body, lesson)
    if sents:
        intro = intro_of(sents)
        core = pick_def(sents, 4)
        hard = pick_mark(sents, 5)
        err = pick_warn(sents, 2)
        points = []
        seen = set()
        for s in core + hard:
            if s not in seen:
                seen.add(s); points.append(s)
        h = ''
        if intro:
            h += '<h4>📖 课文导学</h4><p>' + '。'.join(intro) + '</p>'
        if core:
            h += '<h4>🧩 核心概念</h4><ul class="lp">' + ''.join(f'<li>{esc(s)}</li>' for s in core) + '</ul>'
        if hard:
            h += '<h4>🎯 重难点突破</h4><ul class="lp">' + ''.join(f'<li>{esc(s)}</li>' for s in hard) + '</ul>'
        if err:
            h += '<h4>⚠️ 易错警示</h4><ul class="lp warn">' + ''.join(f'<li>{esc(s)}</li>' for s in err) + '</ul>'
        if points:
            h += '<h4>🗺 知识结构</h4><ol class="lpo">' + ''.join(f'<li>{esc(s)}</li>' for s in points) + '</ol>'
        if h:
            return title, h
    # 兜底：关键词包
    kw = KWBAG.get(lesson, '')
    if kw:
        items = [k for k in re.split(r'[/／]', kw) if k.strip()]
        h = '<h4>🧩 核心要点</h4><ul class="lp">' + ''.join(f'<li>{esc(k.strip())}</li>' for k in items) + '</ul>'
        h += '<p class="muted">本课课本正文暂未收录，以上为课标核心要点；配套题目见题库同课次。</p>'
        return title, h
    return title, '<p class="muted">本课详细知识正在整理，配套题目与知识点卡见题库同课次。</p>'


def unit_of(bk, lesson):
    book = bk.split('|')[1].replace('选择性必修', '选必')
    return f'{book}·{lesson}'


# ---- 主流程 ----
n_pages = 0
for bk, meta in idx.items():
    subj_cn = SUBJ[bk.split('|')[0]]
    bookname = bk.split('|')[1]
    if '选必' in bookname and bk.split('|')[0] != '地理':
        vol = '高考·选必' if bk.split('|')[0] in ('思想政治', '历史') else '高考·综合'
    elif '选必' in bookname:
        vol = '高考·综合'
    else:
        vol = '高考·综合'
    leaf = d['subjects'][subj_cn][vol]
    units = leaf.setdefault('units', {})
    texts = lt.get(bk, {})
    for lesson in meta['lessons']:
        unit = unit_of(bk, lesson)
        title, html = build_page(bookname, lesson, texts.get(lesson, ''))
        units[unit] = {'title': title, 'summary': html}
        n_pages += 1

json.dump(d, open('site/content.json', 'w', encoding='utf-8'),
          ensure_ascii=False, separators=(',', ':'))
print(f'生成同步学习详情页 {n_pages} 个')
# 统计每卷单元数
cnt = {}
for subj, vols in d['subjects'].items():
    for vol, v in vols.items():
        if isinstance(v, dict) and v.get('units'):
            cnt[f'{subj}/{vol}'] = len(v['units'])
for k, n in sorted(cnt.items()):
    print('  ', k, n)
