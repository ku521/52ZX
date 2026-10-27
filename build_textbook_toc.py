# -*- coding: utf-8 -*-
"""下载智慧平台统编版课本 PDF 并提取目录（课级），生成 books/textbook_index.json"""
import json, os, re, subprocess, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pypdf import PdfReader

# 平台已删除 PDF（403）的课本，内置权威目录（统编版2019，公开稳定）
FALLBACK_TOC = {
    '思想政治|必修3_政法': [
        '第一课 历史和人民的选择', '第二课 中国共产党的先进性', '第三课 坚持和加强党的全面领导',
        '第四课 人民民主专政的社会主义国家', '第五课 我国的根本政治制度', '第六课 我国的基本政治制度',
        '第七课 治国理政的基本方式', '第八课 法治中国建设', '第九课 全面依法治国的基本要求'],
    '思想政治|必修4_哲文': [
        '第一课 时代精神的精华', '第二课 探究世界的本质', '第三课 把握世界的规律',
        '第四课 探索认识的奥秘', '第五课 寻觅社会的真谛', '第六课 实现人生的价值',
        '第七课 继承发展中华优秀传统文化', '第八课 学习借鉴外来文化的优秀成果', '第九课 发展中国特色社会主义文化'],
    '历史|纲要(下)': [
        '第1课 文明的产生与早期发展', '第2课 古代世界的帝国与文明的交流', '第3课 中古时期的欧洲',
        '第4课 中古时期的亚洲', '第5课 古代非洲与美洲', '第6课 全球航路的开辟',
        '第7课 全球联系的初步建立与世界格局的演变', '第8课 欧洲的思想解放运动',
        '第9课 资产阶级革命与资本主义制度的确立', '第10课 影响世界的工业革命',
        '第11课 马克思主义的诞生与传播', '第12课 资本主义世界殖民体系的形成', '第13课 亚非拉民族独立运动',
        '第14课 第一次世界大战与战后国际秩序', '第15课 十月革命的胜利与苏联的社会主义实践',
        '第16课 亚非拉民族民主运动的高涨', '第17课 第二次世界大战与战后国际秩序的形成',
        '第18课 冷战与国际格局的演变', '第19课 社会主义国家的发展与变化',
        '第20课 世界殖民体系的瓦解与新兴国家的发展', '第21课 世界多极化与经济全球化',
        '第22课 和平发展合作共赢的时代潮流'],
}

def clean_lessons(lessons):
    """清洗课节标题：截断杂讯、去页码残留、拼回断行、丢弃纯杂项条目"""
    out = []
    junk = r'活动课|问题研究|科学家访谈|附录|探究・实践|探究·实践|生物科技进展|科学・技术・社会|科学·技术·社会|与生物学有关的职业|拓展视野|仅供个人学习|目\s*录'
    for t in lessons:
        t = re.split(junk, t)[0]                            # 截断杂讯
        t = re.sub(r'[\s.…·]*\d+[\s.…·]*$', '', t)          # 尾部页码
        t = re.sub(r'\s+', '', t)                            # 内部空白
        t = re.sub(r'^[.…·、,，]+', '', t)
        fix = {
            '第四课只有坚持和发展中国特色社会主义才能实现中华民族伟大复兴': '第四课只有坚持和发展中国特色社会主义才能实现中华民族伟大复兴',
            '第四课只有坚持和发展中国特色社会主义才能实现': '第四课只有坚持和发展中国特色社会主义才能实现中华民族伟大复兴',
            '第21课南京国民政府的统治和中国共产党开辟': '第21课南京国民政府的统治和中国共产党开辟革命新道路',
        }
        t = fix.get(t, t)
        if not t or len(t) < 5:
            continue
        if t not in out:
            out.append(t)
    return out

ids = json.load(open('tchmaterial_ids.json', encoding='utf-8'))
os.makedirs('books', exist_ok=True)

# 需要：政治必修1-4+选必1-3，历史纲要上下+选必1-3，地理必修1/2+选必1/2，生物全部
NEED = {
    '思想政治': ['必修1 中国特色社会主义', '必修2 经济与社会', '必修3 政治与法治', '必修4 哲学与文化',
               '选择性必修1 当代国际政治与经济', '选择性必修2 法律与生活', '选择性必修3 逻辑与思维'],
    '历史': ['必修 中外历史纲要（上）', '必修 中外历史纲要（下）',
            '选择性必修1 国家制度与社会治理', '选择性必修2 经济与社会生活', '选择性必修3 文化交流与传播'],
    '地理': ['必修 第一册', '必修 第二册', '选择性必修1 自然地理基础', '选择性必修2 区域发展'],
    '生物学': list(ids.get('生物学', {}).keys()),
}

def short_name(subj, name):
    n = name.replace('（', '(').replace('）', ')')
    if subj == '思想政治':
        return n.replace('中国特色社会主义', '中特').replace('经济与社会', '经社') \
                .replace('政治与法治', '政法').replace('哲学与文化', '哲文') \
                .replace('当代国际政治与经济', '国际政经').replace('法律与生活', '法律') \
                .replace('逻辑与思维', '逻辑')
    if subj == '历史':
        return n.replace('必修 中外历史纲要', '纲要').replace('选择性必修', '选必')
    if subj == '地理':
        return n.replace('必修 第一册', '必修一').replace('必修 第二册', '必修二').replace('选择性必修', '选必')
    return n.replace('必修1 ', '必修1·').replace('必修2 ', '必修2·').replace('选择性必修', '选必')

def real_pdf_url(url):
    """从通用 URL 提取 contentId，经详情接口拿真实 PDF 地址"""
    m = re.search(r'/esp/assets/([0-9a-f-]{36})\.pkg/', url)
    if not m:
        return url
    cid = m.group(1)
    det = f'https://s-file-2.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{cid}.json'
    try:
        d = json.loads(subprocess.run(['curl', '-sL', '--max-time', '30', det],
                                      capture_output=True, text=True).stdout)
        for ti in d.get('ti_items', []):
            if ti.get('ti_format') == 'pdf':
                return ti['ti_storages'][0].replace('-private', '')
    except Exception:
        pass
    return url

def download(subj, name, url):
    fn = 'books/' + short_name(subj, name).replace(' ', '_').replace('/', '_') + '.pdf'
    if not os.path.exists(fn) or os.path.getsize(fn) < 100000:
        real = real_pdf_url(url)
        r = subprocess.run(['curl', '-sL', '--max-time', '180', '-o', fn, real])
        if r.returncode != 0 or not os.path.exists(fn) or os.path.getsize(fn) < 100000:
            # 备用：换 r2 镜像
            url2 = real.replace('r1-ndr', 'r2-ndr')
            subprocess.run(['curl', '-sL', '--max-time', '180', '-o', fn, url2])
    return fn

def norm_toc(t):
    return re.sub(r'第\s*([一二三四五六七八九十百0-9]+)\s*(课|节|章)', r'第\1\2', t)

def extract_toc(fn):
    """返回 (toc_text, pages)。toc_pages: 前10页中含>=3个 课/节/章 标记的页"""
    r = PdfReader(fn)
    pages = []
    for p in r.pages:
        try:
            pages.append(p.extract_text() or '')
        except Exception:
            pages.append('')
    npages = [norm_toc(t) for t in pages]
    pat = re.compile(r'第[一二三四五六七八九十百0-9]+(课|节|章)')
    toc_pages = [i for i, t in enumerate(npages[:10]) if len(pat.findall(t)) >= 3]
    return '\n'.join(npages[i] for i in toc_pages), pages

def parse_toc(toc_text, subj):
    """顺序标记扫描：每个 第X课/第X章/第X节 标记后的第一行有效文本即标题"""
    unit_pat = r'第[一二三四五六七八九十百0-9]+单元|综合探究|活动课|问题研究|科学家访谈|仅供个人学习'
    mk = r'第([一二三四五六七八九十百0-9]+)(课|节|章)'
    out, zhang = [], ''
    matches = list(re.finditer(mk, toc_text))
    for i, m in enumerate(matches):
        num, kind = m.group(1), m.group(2)
        seg = toc_text[m.end(): matches[i+1].start() if i+1 < len(matches) else len(toc_text)]
        seg = re.split(unit_pat, seg)[0]
        # 逐行找第一行有效标题（跳过纯页码/点线行）
        title = ''
        for line in seg.split('\n'):
            line2 = re.sub(r'[\s.…·]*\d+[\s.…·]*$', '', line.strip())
            line2 = re.sub(r'[\s.…·]+', '', line2)
            cjk = len(re.findall(r'[\u4e00-\u9fff]', line2))
            if cjk >= 3:
                title = line2
                break
        if kind == '章':
            zhang = f'第{num}章'
            continue
        if not title or '目录' in title:
            continue
        if kind == '课':
            out.append(f'第{num}课 {title}')
        else:
            out.append((zhang + '·' if zhang else '') + f'第{num}节 {title}')
    seen, res = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res

index = {}
for subj, names in NEED.items():
    for name in names:
        if name not in ids.get(subj, {}):
            print(f'!! 缺少 {subj} {name}')
            continue
        key = short_name(subj, name)
        bkkey = f'{subj}|{key}'
        if bkkey in FALLBACK_TOC:
            index[bkkey] = {'file': '', 'lessons': FALLBACK_TOC[bkkey], 'npages': 0}
            print(f'== {subj} / {key}: 内置目录 {len(FALLBACK_TOC[bkkey])}课')
            continue
        fn = download(subj, name, ids[subj][name])
        size = os.path.getsize(fn) if os.path.exists(fn) else 0
        if size < 100000:
            print(f'!! 下载失败 {key} ({size}B)')
            continue
        toc_text, pages = extract_toc(fn)
        lessons = clean_lessons(parse_toc(toc_text, subj))
        index[f'{subj}|{key}'] = {'file': fn, 'lessons': lessons, 'npages': len(pages)}
        print(f'== {subj} / {key}: {size//1024}KB, {len(pages)}页, 课/节 {len(lessons)}')
        for t in lessons:
            print('   ', t)

json.dump(index, open('books/textbook_index.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('\ntextbook_index.json 完成')
