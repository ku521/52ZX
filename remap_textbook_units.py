# -*- coding: utf-8 -*-
"""把高考·综合/高考·选必卷题目按课本课次重新挂单元。
打分：题干+选项+解析 的字符 bigram 与课本每课正文的重叠度 + 课标题 bigram 加权。
低分保留原单元兜底。产出 books/lesson_text.json 缓存。"""
import json, os, re, io, sys
from collections import Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pypdf import PdfReader

TOC_SKIP = 8          # 目录多在前8页
MIN_SCORE = 0.055     # 最低置信分，低于则保留原单元

# 纲要(下)无PDF正文，按课补充知识关键词包（用于匹配打分）
KWBAG = {
    '第1课 文明的产生与早期发展': '两河流域 苏美尔 楔形文字 汉谟拉比 法老 金字塔 象形文字 古埃及 尼罗河 种姓制度 雅典 梭伦 希腊城邦 民主政治 罗马帝国 文明产生',
    '第2课 古代世界的帝国与文明的交流': '波斯帝国 君主专制 行省 亚历山大 东征 希腊化 罗马帝国 丝绸之路 文明交流 帝国更迭',
    '第3课 中古时期的欧洲': '封君封臣 庄园 农奴 自治城市 基督教会 王权 拜占庭 查士丁尼 罗马法 基辅罗斯 东正教 中古 封建欧洲',
    '第4课 中古时期的亚洲': '阿拉伯帝国 哈里发 伊斯兰教 奥斯曼帝国 苏丹 伊斯坦布尔 笈多帝国 德里苏丹 日本 大化改新 幕府 将军 中古亚洲',
    '第5课 古代非洲与美洲': '桑海 加纳 马里 廷巴克图 桑给巴尔 大津巴布韦 玛雅 阿兹特克 印加 美洲文明 古代非洲',
    '第6课 全球航路的开辟': '新航路 迪亚士 达伽马 哥伦布 麦哲伦 葡萄牙 西班牙 淘金 航海 远洋 开辟',
    '第7课 全球联系的初步建立与世界格局的演变': '物种交流 玉米 甘薯 马铃薯 人口迁移 三角贸易 黑奴 殖民掠夺 美洲 白银 天花 病原体 哥伦布大交换 全球联系',
    '第8课 欧洲的思想解放运动': '文艺复兴 人文主义 但丁 薄伽丘 达芬奇 宗教改革 路德 因信称义 科学革命 哥白尼 牛顿 启蒙运动 伏尔泰 孟德斯鸠 卢梭 理性 天赋人权 思想解放',
    '第9课 资产阶级革命与资本主义制度的确立': '英国革命 光荣革命 权利法案 君主立宪 内阁 美国独立 1787宪法 联邦制 法国大革命 人权宣言 拿破仑 民主共和 资本主义制度 议会 资产阶级革命',
    '第10课 影响世界的工业革命': '工业革命 珍妮纺纱机 瓦特 蒸汽机 工厂制度 城市化 阶级 自由主义 空想社会主义 第一次工业革命 第二次工业革命 垄断 影响世界',
    '第11课 马克思主义的诞生与传播': '马克思 恩格斯 共产党宣言 唯物史观 剩余价值 巴黎公社 工人运动 第一国际 科学社会主义 马克思主义诞生',
    '第12课 资本主义世界殖民体系的形成': '殖民体系 拉丁美洲 独立 侵略 亚洲 半殖民地 柏林会议 非洲 瓜分 资本输出 殖民体系形成',
    '第13课 亚非拉民族独立运动': '民族独立 海地 玻利瓦尔 独立运动 印度 国大党 提拉克 伊朗 立宪革命 苏丹 马赫迪 埃及 亚洲觉醒 亚非拉民族独立',
    '第14课 第一次世界大战与战后国际秩序': '一战 萨拉热窝 三国同盟 三国协约 凡尔登 索姆河 凡尔赛 华盛顿会议 国际联盟 委任统治 民族自决 战后国际秩序 第一次世界大战',
    '第15课 十月革命的胜利与苏联的社会主义实践': '十月革命 列宁 布尔什维克 四月提纲 战时共产主义 新经济政策 苏联 斯大林模式 工业化 农业集体化 十月革命胜利',
    '第16课 亚非拉民族民主运动的高涨': '非暴力不合作 甘地 印度 桑地诺 卡德纳斯 墨西哥 亚非拉 民主运动 高涨',
    '第17课 第二次世界大战与战后国际秩序的形成': '二战 法西斯 绥靖 慕尼黑 敦刻尔克 斯大林格勒 珍珠港 中途岛 开罗宣言 德黑兰 雅尔塔 波茨坦 联合国 纽伦堡 东京审判 第二次世界大战',
    '第18课 冷战与国际格局的演变': '冷战 杜鲁门主义 马歇尔计划 北约 华约 柏林危机 古巴导弹危机 朝鲜战争 越南战争 两极格局 多极化 不结盟 国际格局',
    '第19课 社会主义国家的发展与变化': '赫鲁晓夫 勃列日涅夫 戈尔巴乔夫 改革 东欧 南斯拉夫 苏联解体 社会主义国家 发展与变化',
    '第20课 世界殖民体系的瓦解与新兴国家的发展': '殖民体系瓦解 印度独立 纳赛尔 非洲独立 万隆 不结盟运动 第三世界 新兴国家 发展中国家 殖民体系瓦解',
    '第21课 世界多极化与经济全球化': '多极化 经济全球化 跨国公司 区域化 欧盟 亚太经合 互联网 现代信息技术 文化多样性 逆全球化 世界多极化',
    '第22课 和平发展合作共赢的时代潮流': '和平与发展 人道主义 联合国维和 全球治理 人类命运共同体 气候变化 粮食安全 恐怖主义 金融风险 可持续发展 合作共赢',
}

idx = json.load(open('books/textbook_index.json', encoding='utf-8'))

# ---------- 提取每课正文 ----------
def build_lesson_text():
    cache = {}
    if os.path.exists('books/lesson_text.json'):
        cache = json.load(open('books/lesson_text.json', encoding='utf-8'))
    changed = False
    for bk, meta in idx.items():
        if bk in cache:
            continue
        if not meta.get('file'):
            # 内置目录课本（平台已删PDF）：无正文，仅靠课标题匹配
            cache[bk] = {t: '' for t in meta['lessons']}
            print(f'  跳过正文: {bk} (内置目录, {len(meta["lessons"])}课)')
            continue
        r = PdfReader(meta['file'])
        pages = []
        for p in r.pages:
            try:
                pages.append(re.sub(r'\s+', '', p.extract_text() or ''))
            except Exception:
                pages.append('')
        lessons = meta['lessons']
        # 定位每课起始页：在目录页之后找标题首次出现（章·节 取节部分探测）
        starts = []
        for t in lessons:
            probe_src = t.split('·')[-1] if '·' in t else t
            probe = probe_src[:10]
            pg = None
            for i in range(TOC_SKIP, len(pages)):
                if probe in pages[i]:
                    pg = i
                    break
            starts.append(pg)
        texts = {}
        for j, t in enumerate(lessons):
            s = starts[j]
            if s is None:
                texts[t] = ''
                continue
            # 结束页 = 下一个已定位课的起始页
            e = next((x for x in starts[j+1:] if x is not None), len(pages))
            texts[t] = ''.join(pages[s:e])[:6000]
        cache[bk] = texts
        changed = True
        print(f'  提取正文: {bk} ({sum(1 for v in texts.values() if v)}/{len(texts)}课命中)')
    if changed:
        json.dump(cache, open('books/lesson_text.json', 'w', encoding='utf-8'),
                  ensure_ascii=False)
    return cache

LESSON_TEXT = build_lesson_text()

# 历史中外史路由关键词
HIS_WORLD_KW = ['罗马', '希腊', '雅典', '埃及', '两河', '巴比伦', '阿拉伯', '奥斯曼', '拜占庭',
                '玛雅', '印加', '阿兹特克', '文艺复兴', '启蒙运动', '人文主义', '宗教改革', '路德',
                '新航路', '哥伦布', '麦哲伦', '迪亚士', '殖民', '三角贸易', '黑奴', '工业革命',
                '蒸汽机', '瓦特', '共产党宣言', '巴黎公社', '一战', '第一次世界大战', '二战',
                '第二次世界大战', '法西斯', '纳粹', '冷战', '杜鲁门', '马歇尔', '北约', '华约',
                '古巴导弹', '凡尔赛', '国际联盟', '联合国', '十月革命', '列宁', '斯大林',
                '赫鲁晓夫', '勃列日涅夫', '戈尔巴乔夫', '苏联', '罗斯福', '大危机', '经济危机',
                '多极化', '全球化', '欧盟', '欧共体', '万隆', '不结盟', '甘地', '非暴力', '拿破仑',
                '权利法案', '君主立宪', '人权宣言', '明治', '威尼斯', '君士坦丁堡', '世界市场',
                '德国', '法国大革命', '英国资产阶级', '美洲', '非洲', '全球']
HIS_CHINA_KW = ['我国', '中国', '朝廷', '皇帝', '科举', '租庸', '两税', '一条鞭', '洋务', '条约',
                '租界', '抗战', '抗日战争', '解放战争', '新中国', '中华人民共和国', '改革开放',
                '十一届三中', '一国两制', '特区', '社会主义建设', '三大改造', '一五计划', '文革',
                '大跃进', '人民公社', '太平天国', '义和团', '戊戌', '甲午', '鸦片战争', '辛亥',
                '五四', '中共', '国民党', '北洋', '军阀', '长征', '遵义', '井冈山', '南京国民政府',
                '通商口岸', '领事', '海关', '儒', '理学', '郡县', '行省', '内阁', '军机处']

def his_route(qtext, bklist):
    """历史综合卷：世界史题排除纲要上，中国史题排除纲要下"""
    ws = sum(qtext.count(k) for k in HIS_WORLD_KW)
    cs = sum(qtext.count(k) for k in HIS_CHINA_KW)
    if ws > cs:
        return [b for b in bklist if '纲要(上)' not in b]
    if cs >= ws:
        return [b for b in bklist if '纲要(下)' not in b]
    return bklist

# 科目/卷 → 参与匹配的课本
BOOK_MAP = {
    ('政治', '高考·综合'): ['思想政治|必修1 中特', '思想政治|必修2 经社', '思想政治|必修3 政法', '思想政治|必修4 哲文'],
    ('政治', '高考·选必'): ['思想政治|选择性必修1 国际政经', '思想政治|选择性必修2 法律', '思想政治|选择性必修3 逻辑'],
    ('历史', '高考·综合'): ['历史|纲要(上)', '历史|纲要(下)'],
    ('历史', '高考·选必'): ['历史|选必1 国家制度与社会治理', '历史|选必2 经济与社会生活', '历史|选必3 文化交流与传播'],
    ('地理', '高考·综合'): ['地理|必修一', '地理|必修二', '地理|选必2 区域发展'],
    ('生物', '高考·综合'): [k for k in idx if k.startswith('生物学|')],
}

def grams(s, n=2):
    s = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', s)
    return {s[i:i+n] for i in range(len(s)-n+1)} if len(s) >= n else {s} if s else set()

def unit_name(bk, lesson):
    book = bk.split('|')[1].replace('选择性必修', '选必')
    return f'{book}·{lesson}'

# ---------- 主流程 ----------
d = json.load(open('site/content.json', encoding='utf-8'))
qs = d['questions']

# 预编译课本数据：book → [(unit, title_grams, body_grams, body_set)]
books_data = {}
for bk, texts in LESSON_TEXT.items():
    arr = []
    for t, body in texts.items():
        if not body:
            body = KWBAG.get(t, '')
        tg = grams(t)
        bg = grams(body)
        arr.append((unit_name(bk, t), tg, bg, len(bg)))
    books_data[bk] = arr

stats = Counter()
unmapped = []
for q in qs:
    vol, subj = q.get('volume'), q.get('subject')
    bklist = BOOK_MAP.get((subj, vol))
    if not bklist:
        continue
    qtext = q.get('stem', '') + ' ' + json.dumps(q.get('options', {}), ensure_ascii=False) + ' ' + str(q.get('expl', ''))[:800]
    qg = grams(qtext)
    if not qg:
        stats['空题'] += 1
        continue
    if subj == '历史':
        bklist = his_route(qtext, bklist)
    best_u, best_s = None, 0.0
    for bk in bklist:
        for unit, tg, bg, bn in books_data.get(bk, []):
            if not bg:
                continue
            inter = len(qg & bg)
            score = inter / (len(qg) ** 0.5 * bn ** 0.3)
            # 课标题命中加权
            hit = sum(1 for g in tg if g in qtext)
            if hit:
                score += hit * 0.02
            if score > best_s:
                best_s, best_u = score, unit
    if best_u and best_s >= MIN_SCORE:
        stats['已映射'] += 1
        stats[best_u] += 1
        q['unit'] = best_u
    else:
        stats['保留原单元'] += 1
        unmapped.append((q['unit'], q['stem'][:40]))

print('映射统计:', stats['已映射'], '保留兜底:', stats['保留原单元'])
for u, n in sorted(stats.items(), key=lambda x: -x[1]):
    if not isinstance(u, str) or u in ('已映射', '保留原单元', '空题'):
        continue
    print(f'  {u}: {n}')
print('\n兜底样例:')
for u, s in unmapped[:10]:
    print(f'  [{u}] {s}')

json.dump(d, open('site/content.json', 'w', encoding='utf-8'),
          ensure_ascii=False, separators=(',', ':'))
print('content.json 已更新')
