# -*- coding: utf-8 -*-
"""课本正文清洗：从 PDF 提取的噪声文本中析出"可用知识点句子"。
噪声类型：标题串入(第X课/章)、页码残留、习题问句(导语)、序号(①②③)、断头碎片。
供 gen_lesson_pages.py 与 gen_lesson_cards.py 共用，保证详情页与卡片同源。
"""
import re

ART = re.compile(r'[①②③④⑤⑥⑦⑧⑨⑩◆●•·]')
HEAD = re.compile(r'第[一二三四五六七八九十百\d]+[课章节单元]')
PROMPT = re.compile(r'探究|思考|讨论|观察|比较|结合|分析|阅读|如图|下图|上表|如表|右图|左图|谈谈|辨析|概括|列举|指出|请[你]|你能|试试|材料|为什么|如何|怎样|吗')
LEAD_BAD = re.compile(r'^(他们|它|这|其|因此|所以|但是|然而|同时|此外|由此|可见|总之|上述|以上|其中|这时|一般来说|例如|比如|让我们|也就是说|是|叫|即|指|由)')
NUM_HEAD = re.compile(r'^[\u4e00-\u9fff]{0,4}\d+[．.、]\s*')
DEF = re.compile(r'是指|指的是|即[是叫]|称为|叫做|叫作|标志着|本质是|所谓|也就是|意思是|可以理解为|含义是|概念是|这一概念')
MARK = re.compile(r'本质|核心|根本|决定|标志|关键|基础|目的|原则|特点|实质|首要|最重要|内涵|内容包括|中心|支柱|主导|主体|意义|作用|影响|原因在于|前提|保证|主要内容|基本特征|必然要求|体现|反映')
WARN = re.compile(r'易错|注意|混淆|不能|不等于|区别|不同于|误|陷阱|切忌|并非|不等于')
# 子标题/栏目名：用于在中途强制断句，打破"标题+正文"粘连
SUBHEAD = re.compile(r'(第[一二三四五六七八九十百\d]+(课|节|章|单元)|学习聚焦|探究与分享|相关链接|专家点评|学思之窗|历史纵横|资料浏览|方法点拨)')


def clean_lesson_text(body, title=''):
    if not body:
        return []
    # 课标题尾巴（去掉所有"第X课/章/节"及·后的剩余，常作为子标题粘连进正文）
    title_tail = ''
    if title:
        title_tail = re.sub(r'(第[一二三四五六七八九十百\d]+(课|节|章|单元)[·]?)+', '', title).strip()
    body = re.sub(r'仅供个人学习使用，未经授权不得另做用去', '', body)
    body = re.sub(r'仅供个人学习[^。]*', '', body)
    body = re.sub(r'目\s*录', '', body)
    body = ART.sub('。', body)          # 序号当分隔
    body = body.replace('！', '。').replace('？', '。')  # 问/叹句作分隔，知识用陈述句
    # 在嵌入的标题/栏目名前插入断点，打破"标题+正文"粘连
    body = SUBHEAD.sub(lambda m: '。' + m.group(1), body)
    out = []
    for s in re.split(r'[。；\n]', body):
        s = s.strip()
        if not s:
            continue
        s = re.sub(r'^(第[一二三四五六七八九十百\d]+(课|节|章|单元)[·]?)+', '', s).strip()
        if title_tail and s.startswith(title_tail):
            s = s[len(title_tail):].strip()
        # 循环剥离“XX对地球的影响”类子标题（PDF 子标题粘连正文）
        while True:
            ns = re.sub(r'^[\u4e00-\u9fff]{2,10}对(地球|地表|人类|生物|社会|环境)的影响', '', s).strip()
            if ns == s:
                break
            s = ns
        s = re.sub(r'^\d{5,}(?=年)', lambda m: m.group(0)[-4:], s).strip()  # 仅"页码+年份"(5位+)保留末4位年
        s = NUM_HEAD.sub('', s).strip()
        s = re.sub(r'^[，、；\s]+', '', s)
        if not s:
            continue
        if HEAD.search(s) and len(s) < 32:   # 整句是标题
            continue
        if PROMPT.search(s):
            continue
        if re.search(r'图\d|表\d|学习聚焦|探究与分享|相关链接|专家点评|学思之窗|历史纵横', s):
            continue
        if re.match(r'^[\d\s.…·]+$', s):
            continue
        if LEAD_BAD.match(s):
            continue
        # 子标题碎片：以"…的影响/特点/形成/变化/意义"开头（PDF 标题粘连正文）
        if re.match(r'^(对地球的影响|对地表的影响|的特点|的形成|的变化|的意义|的影响)', s):
            continue
        if len(s) < 14 or len(s) > 130:
            continue
        out.append(s)
    seen, res = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            res.append(s)
    return res


def pick_def(sents, n=4):
    return [s for s in sents if DEF.search(s)][:n]


def pick_mark(sents, n=5):
    return [s for s in sents if MARK.search(s) and not DEF.search(s)][:n]


def pick_warn(sents, n=2):
    return [s for s in sents if WARN.search(s)][:n]


def intro_of(sents, n=2, maxlen=110):
    """导学：取前 1-2 条干净陈述句；跳过框标题碎片（科学内涵/意味着什么/进入新时代/伟大复兴等）。"""
    REJ = re.compile(r'意味着什么|科学内涵|进入新时代|伟大复兴|对地球的影响|对地表的影响|第[一二三四五六七八九十]课|只有坚持')
    out = []
    for s in sents:
        if 22 <= len(s) <= maxlen and not LEAD_BAD.match(s) and not REJ.search(s):
            out.append(s)
            if len(out) >= n:
                break
    if not out:  # 退而求其次：任取一条干净陈述
        for s in sents:
            if 22 <= len(s) <= 130 and not LEAD_BAD.match(s):
                out = [s[:maxlen]]
                break
    return out
