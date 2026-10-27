# -*- coding: utf-8 -*-
"""
重做 高二+高三 共 11 册的 mindmap（思维导图）SVG。
统一布局：左「根(册名)」 + 中「模块(章节)」 + 右「子点(考点，全部画框)」。
节点数对齐高一册水平(15~25)，并修复地理·高考·综合 的内容错配与标题截断。
仅改动 subjects[科目][册]['mindmap']，不动 summary / cards / questions。
"""
import json, os, re, xml.dom.minidom as M

P = os.path.join('site', 'content.json')
d = json.load(open(P, encoding='utf-8'))
sub = d['subjects']

def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def gen_mindmap(root_label, modules, maxw=780):
    root_w, root_h = 210, 42
    mod_w, mod_h = 200, 36
    sub_w, sub_h = 235, 30
    gx = 45          # 根右 -> 模块左 间距
    gx2 = 45         # 模块右 -> 子点左 间距
    gap_mod = 16
    gap_sub = 8
    root_x = 20
    mod_x = root_x + root_w + gx
    sub_x = mod_x + mod_w + gx2

    # 每模块占高 = 其子点总高 与 模块最小高 的较大者 + 内边距
    blocks = []
    for mtitle, subs in modules:
        subs_h = len(subs) * sub_h + (len(subs) - 1) * gap_sub
        h = max(mod_h, subs_h) + 16
        blocks.append((mtitle, subs, h))

    total_h = sum(h for _, _, h in blocks) + gap_mod * (len(blocks) - 1) + 40
    root_y = total_h / 2.0 - root_h / 2
    root_cx = root_x + root_w / 2.0
    root_cy = root_y + root_h / 2.0

    svg = [f'<svg viewBox="0 0 {maxw} {int(total_h)}" width="100%" preserveAspectRatio="xMinYMin meet" '
           f'style="max-width:780px;height:auto;font-family:\'Microsoft YaHei\',\'微软雅黑\',sans-serif">']

    def path_elbow(x1, y1, x2, y2, stroke, w, op):
        midx = (x1 + x2) / 2.0
        return (f'<path d="M{x1:.1f},{y1:.1f} C{midx:.1f},{y1:.1f} {midx:.1f},{y2:.1f} '
                f'{x2:.1f},{y2:.1f}" fill="none" stroke="{stroke}" stroke-width="{w}" stroke-opacity="{op}"/>')

    # —— 连线（先画，置于框之下）——
    y = 20.0
    for mtitle, subs, h in blocks:
        mod_y = y
        mod_cy = mod_y + mod_h / 2.0
        svg.append(path_elbow(root_x + root_w, root_cy, mod_x, mod_cy, '#1565c0', 1.6, 0.6))
        subs_h = len(subs) * sub_h + (len(subs) - 1) * gap_sub
        sy = mod_y + (h - subs_h) / 2.0
        for si, stxt in enumerate(subs):
            s_y = sy + si * (sub_h + gap_sub)
            s_cy = s_y + sub_h / 2.0
            svg.append(path_elbow(mod_x + mod_w, mod_cy, sub_x, s_cy, '#90a4ae', 1.3, 0.5))
        y += h + gap_mod

    # —— 模块框 + 子点框 + 文本 ——
    y = 20.0
    for mtitle, subs, h in blocks:
        mod_y = y
        mod_cy = mod_y + mod_h / 2.0
        svg.append(f'<rect x="{mod_x}" y="{mod_y:.1f}" width="{mod_w}" height="{mod_h}" rx="8" '
                   f'fill="#e8f1fb" stroke="#1565c0" stroke-width="1.4"/>')
        svg.append(f'<text x="{mod_x + mod_w/2.0:.1f}" y="{mod_cy+4.5:.1f}" text-anchor="middle" '
                   f'font-size="13" fill="#0d47a1" font-weight="600">{esc(mtitle)}</text>')
        subs_h = len(subs) * sub_h + (len(subs) - 1) * gap_sub
        sy = mod_y + (h - subs_h) / 2.0
        for si, stxt in enumerate(subs):
            s_y = sy + si * (sub_h + gap_sub)
            s_cy = s_y + sub_h / 2.0
            svg.append(f'<rect x="{sub_x}" y="{s_y:.1f}" width="{sub_w}" height="{sub_h}" rx="7" '
                       f'fill="#ffffff" stroke="#90a4ae" stroke-width="1"/>')
            svg.append(f'<text x="{sub_x + sub_w/2.0:.1f}" y="{s_cy+4:.1f}" text-anchor="middle" '
                       f'font-size="12" fill="#1a1a1a">{esc(stxt)}</text>')
        y += h + gap_mod

    # —— 根框（最上层）——
    svg.append(f'<rect x="{root_x}" y="{root_y:.1f}" width="{root_w}" height="{root_h}" rx="10" fill="#1565c0"/>')
    svg.append(f'<text x="{root_cx:.1f}" y="{root_cy+5:.1f}" text-anchor="middle" '
               f'font-size="14.5" fill="#fff" font-weight="700">{esc(root_label)}</text>')
    svg.append('</svg>')
    return ''.join(svg)

# ============ 11 册课程树（准确对应统编/人教版课纲）============
TREES = {
  # ---------- 政治 ----------
  ('政治','高二·必修3'): ('必修3 政治与法治', [
      ('党的领导', ['党的性质与宗旨', '执政理念与方式', '全面从严治党']),
      ('人民当家作主', ['人民代表大会制度', '政协与协商民主', '基层群众自治']),
      ('民族与宗教', ['民族平等团结互助', '民族区域自治制度', '宗教工作基本方针']),
      ('全面依法治国', ['总目标与原则', '科学立法', '严格执法·公正司法', '全民守法']),
  ]),
  ('政治','高二·必修4'): ('必修4 哲学与文化', [
      ('辩证唯物论', ['物质与意识的辩证', '运动与规律', '一切从实际出发']),
      ('唯物辩证法', ['联系观', '发展观', '矛盾观', '辩证否定观']),
      ('认识论', ['实践与认识', '真理与谬误']),
      ('历史唯物主义', ['社会存在与社会意识', '人民群众是主体', '价值观与人生价值']),
      ('文化传承', ['文化的功能', '中华优秀传统文化', '文化强国与文化自信']),
  ]),
  ('政治','高考·综合'): ('政治·高考·综合', [
      ('必修1 中特', ['社会形态更替规律', '中国特色社会主义新时代', '改革开放·两个百年', '中国梦与战略安排']),
      ('必修2 经社', ['基本经济制度', '新发展理念', '高质量发展', '共同富裕·乡村振兴']),
      ('必修3 政法', ['党的领导', '人民民主专政', '人大·全过程民主', '全面依法治国']),
      ('必修4 哲文', ['唯物论', '辩证法', '认识论', '历史唯物主义', '文化传承与创新']),
  ]),
  ('政治','高考·选必'): ('政治·高考·选必', [
      ('选必1 国际政治', ['主权国家与国际组织', '联合国与中国外交', '和平与发展主题', '经济全球化·多边贸易']),
      ('选必2 法律生活', ['民事行为能力', '合同订立与履行', '婚姻与继承', '侵权责任', '诉讼与仲裁']),
      ('选必3 逻辑思维', ['概念·判断·推理', '演绎推理', '归纳与类比', '辩证思维', '创新思维方法']),
  ]),
  # ---------- 历史 ----------
  ('历史','高二·纲要下'): ('纲要下 世界史', [
      ('古代文明', ['大河与海洋文明', '中古欧洲封建', '阿拉伯与伊斯兰']),
      ('全球航路', ['新航路开辟', '早期殖民扩张', '商业与价格革命']),
      ('资本主义制度', ['英法美革命', '工业革命', '资本主义世界体系']),
      ('工人与马克思主义', ['马克思主义诞生', '巴黎公社', '第二国际']),
      ('世界大战', ['第一次世界大战', '凡尔赛-华盛顿体系', '第二次世界大战']),
      ('冷战与格局', ['雅尔塔体系', '冷战对峙', '多极化趋势']),
      ('当代世界', ['经济全球化', '联合国与第三世界', '科技与社会变迁']),
  ]),
  ('历史','高考·综合'): ('历史·高考·综合', [
      ('中国近现代', ['列强侵略与民族危机', '中国人民抗争探索', '辛亥革命与民国', '新民主主义革命胜利', '改革开放与中国特色社会主义']),
      ('世界史主线', ['资本主义世界体系', '国际关系与战争', '冷战与多极化', '经济全球化']),
      ('史学素养', ['唯物史观', '史料实证', '历史解释', '时空观念']),
      ('答题技法', ['背景原因分析', '影响意义类', '比较异同类', '论证评价类']),
  ]),
  ('历史','高考·选必'): ('历史·高考·选必', [
      ('选必1 制度治理', ['历代政治制度', '官员选拔与管理', '法律与教化', '基层治理·赋税制度']),
      ('选必2 经济生活', ['食物生产变迁', '生产工具进步', '商业贸易发展', '交通与城市', '医疗与文教']),
      ('选必3 文化交流', ['中华优秀传统文化', '中西文化碰撞', '人口迁徙与移民', '商路·贸易与文化交流', '文化遗产保护']),
  ]),
  # ---------- 地理 ----------
  ('地理','高二·必修二'): ('必修二 人文地理', [
      ('人口', ['人口增长与结构', '人口迁移', '环境人口容量']),
      ('城镇与乡村', ['城市化进程', '城市空间结构', '城乡景观差异']),
      ('产业区位', ['农业区位因素', '工业区位因素', '服务业区位']),
      ('交通运输', ['运输方式选择', '交通枢纽布局', '交通对聚落影响']),
      ('区域发展与环境', ['生态脆弱区治理', '资源型城市转型', '流域综合治理']),
      ('区域协调', ['长江经济带', '一带一路', '主体功能区']),
  ]),
  ('地理','高考·综合'): ('地理·高考·综合', [
      ('自然地理', ['地球运动', '大气环流与气候', '水文与水循环', '地貌与内外力', '土壤与植被']),
      ('人文地理', ['人口与城市', '农业区位', '工业区位', '服务业与交通']),
      ('区域与生态', ['区域发展分析', '生态环境建设', '资源开发与调配', '灾害防治']),
      ('读图与技法', ['等值线判读', '区位分析方法', '成因类答题模板', '意义措施类模板']),
  ]),
  # ---------- 生物 ----------
  ('生物','高二·遗传与进化'): ('遗传与进化', [
      ('遗传定律', ['分离定律', '自由组合定律', '伴性遗传']),
      ('减数分裂', ['减数分裂过程', '配子形成与受精']),
      ('遗传物质', ['DNA分子结构', 'DNA复制', '转录与翻译']),
      ('变异与育种', ['基因突变', '染色体变异', '杂交与诱变育种', '基因工程育种']),
      ('生物进化', ['现代进化理论', '物种形成', '生物多样性']),
  ]),
  ('生物','高考·综合'): ('生物·高考·综合', [
      ('必修1 分子与细胞', ['细胞结构与跨膜运输', '酶·ATP·呼吸', '光合作用', '有丝与减数分裂']),
      ('必修2 遗传与进化', ['分离与自由组合', '伴性遗传', 'DNA·转录翻译', '变异与进化']),
      ('选必1 稳态调节', ['内环境稳态', '神经调节', '体液调节', '免疫调节', '植物激素']),
      ('选必2 生物与环境', ['种群与群落', '生态系统结构', '能量流动与物质循环', '生物多样性保护']),
      ('选必3 生物技术', ['发酵工程', '细胞工程', '基因工程·PCR', '胚胎与单克隆']),
  ]),
}

# ============ 写入 ============
done, skipped = [], []
for (s, v), (root_label, modules) in TREES.items():
    if s not in sub or v not in sub[s]:
        skipped.append(f'{s}·{v}(册别不存在)')
        continue
    mm = gen_mindmap(root_label, modules)
    # 校验 XML 合法
    M.parseString(mm)
    sub[s][v]['mindmap'] = mm
    nrect = mm.count('<rect')
    done.append(f'{s}·{v}: {nrect}框/{len(modules)}模块/{sum(len(x[1]) for x in modules)}子点')

json.dump(d, open(P, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('写入完成。')
print('\n'.join('  ✓ ' + x for x in done))
if skipped:
    print('\n跳过:')
    print('\n'.join('  ! ' + x for x in skipped))
