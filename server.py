#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政史地学习网站 · 服务端
------------------------------------------------------------
安全设计（核心）：
  1. Turso 数据库令牌、管理员密码 **只存在本机 server_config.json**，
     永远不下发到浏览器；前端 HTML 里没有任何密钥。
  2. 注册即自动开通（approved）且免费使用，无需审核；令牌仅存本机。
  3. 登录态 = HMAC 签名的 HttpOnly Cookie，前端 JS 读不到。
  4. 题库/资料内容不内嵌在 HTML，登录且审核通过且已开会员后才由
     /api/content 下发（带 no-store，禁止缓存）。
运行：python server.py            （默认 8080 端口）
      python server.py 9000       （指定端口）
仅用 Python 标准库，无需 pip 安装任何东西。
"""

import base64
import gzip
import io
import zipfile
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================================================ 背诵语音过关：文本相似度评分
# 规范化：转小写、去掉空白与中英文标点，仅保留字母/数字/汉字，便于稳定比对
_REC_PUNCT = set(
    " \t\n\r　!\"#$%&'()*+,-./:;<=>?@[]^_`{|}~，。、；：？！“”‘’（）《》〈〉「」『』【】…—·～"
)
def _rec_norm(s):
    s = (s or "").lower()
    return "".join(ch for ch in s if ch not in _REC_PUNCT)

def _lcs_len(a, b):
    """最长公共子序列长度（滚动两行 DP），用于字符级相似度。"""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0
    prev = [0] * (m + 1)
    for i in range(n):
        ai = a[i]
        cur = [0] * (m + 1)
        for j in range(m):
            if ai == b[j]:
                cur[j + 1] = prev[j] + 1
            else:
                cur[j + 1] = prev[j + 1] if prev[j + 1] >= cur[j] else cur[j]
        prev = cur
    return prev[m]

def text_sim(a, b):
    """背诵评分：0~1。= 0.75×字符级 LCS 比例 + 0.25×关键短语覆盖率。
    LCS 比例对漏虚词（的/是/了）和个别错字极不敏感，作为主干；
    关键短语取标准答案中长度>=5 的断句片段，要求出现在背诵文本里，作为补充。"""
    a, b = _rec_norm(a), _rec_norm(b)
    if not a or not b:
        return 0.0
    L = _lcs_len(a, b)
    ratio = 2.0 * L / (len(a) + len(b))
    anchors = [x for x in re.split(r"[，。；：！？,.;:!?]+", b) if len(x) >= 5]
    cov = 0.0
    if anchors:
        hit = sum(1 for x in anchors if x in a)
        cov = hit / len(anchors)
    return 0.75 * ratio + 0.25 * cov

_CONTENT_INDEX = None
def _load_card_index():
    """从 content.json 构建 cardKey -> card 索引（进程内缓存一次）。
    cardKey 与前端一致：subject + '@@' + unit按｜取末段 + '@@' + front。"""
    global _CONTENT_INDEX
    if _CONTENT_INDEX is not None:
        return _CONTENT_INDEX
    _CONTENT_INDEX = {}
    if not os.path.exists(CONTENT_PATH):
        return _CONTENT_INDEX
    try:
        with open(CONTENT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in (data.get("cards") or []):
            subj = c.get("subject") or ""
            unit = (c.get("unit") or "").split("｜")[-1]
            front = c.get("front") or ""
            _CONTENT_INDEX[subj + "@@" + unit + "@@" + front] = c
    except Exception as e:
        print("[!] 卡片索引构建失败：", e)
    return _CONTENT_INDEX

BASE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(BASE, "server_config.json")
SITE_DIR = os.path.join(BASE, "site")
INDEX_PATH = os.path.join(SITE_DIR, "index.html")
CONTENT_PATH = os.path.join(SITE_DIR, "content.json")

# 资料接口的 gzip 预压缩缓存。
# 背景：content.json 约 2.2MB，原实现每次请求都实时 gzip(level 9)（单次约 88ms CPU），
# 且为防"一键爬库"又套了一层 base64，导致传输体积从 603KB 膨胀到 804KB（+33%）。
# 现改为：内容文件变更时预生成一份 .gz（约 603KB），请求时直接读缓存并用 HTTP gzip 传输，
# 传输量降 25%、服务端 CPU 归零。防爬能力改由「登录 + 审核 + 单次票据」三层把关保证，
# 不再依赖编码混淆。
_CONTENT_GZ = {"mtime": 0.0, "size": 0, "blob": None}        # 会员版（全量，含答案/解析）
_CONTENT_GZ_FREE = {"mtime": 0.0, "size": 0, "blob": None}   # 免费版（已剥离答案/解析）


def strip_free_content(data):
    """免费用户：剥离答案与解析，仅保留可浏览的题干与知识点。
    - questions / papers[].items：去掉 answer、expl（答案与解析）
    - essays：去掉 points（赋分要点即标准答案）
    - cards / roadmap / memory / subjects / hot：保留（属浏览内容，免费开放）
    这样即便前端被 eval 绕过会员 UI，免费用户本地也根本没有答案数据，无法泄题。
    """
    d = dict(data)
    if isinstance(d.get("questions"), list):
        d["questions"] = [
            {k: v for k, v in q.items() if k not in ("answer", "expl")}
            for q in d["questions"]
        ]
    if isinstance(d.get("essays"), list):
        d["essays"] = [
            {k: v for k, v in e.items() if k != "points"}
            for e in d["essays"]
        ]
    if isinstance(d.get("papers"), list):
        papers = []
        for p in d["papers"]:
            if isinstance(p, dict) and isinstance(p.get("items"), list):
                np = dict(p)
                np["items"] = [
                    {k: v for k, v in it.items() if k not in ("answer", "expl")}
                    for it in p["items"]
                ]
                papers.append(np)
            else:
                papers.append(p)
        d["papers"] = papers
    return d


def content_gzip_blob(free=False):
    """返回 content.json 的 gzip 压缩结果（按 mtime 失效的进程内缓存）。
    free=True 时返回「去答案版」，供未开通会员的免费用户浏览——
    堵塞「前端 eval 把 MEMBER 改成 on 就拿到答案」的绕过路径。"""
    cache = _CONTENT_GZ_FREE if free else _CONTENT_GZ
    try:
        st = os.stat(CONTENT_PATH)
    except OSError:
        return None
    if cache["mtime"] == st.st_mtime and cache["blob"] is not None:
        return cache["blob"]
    raw = None
    if free:
        # 免费版：解析 → 剥离答案 → 重新序列化再压缩
        try:
            with open(CONTENT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = json.dumps(strip_free_content(data), ensure_ascii=False).encode("utf-8")
        except (OSError, ValueError):
            raw = None
        if raw is not None:
            blob = gzip.compress(raw, 6)
            cache.update({"mtime": st.st_mtime, "size": len(blob), "blob": blob})
            return blob
    # 会员版：优先读预生成的 .gz（已是压缩态，勿重复压缩），没有或过期则实时压缩一次并回写
    # 双重校验：mtime + 内容指纹。部署时若文件 mtime 失真（例如 .gz 比源文件旧），
    # 单靠 mtime 会把「旧版资料」的 .gz 当成有效缓存发给用户，故再加一道指纹比对。
    key = CONTENT_PATH + ".gz"
    blob = None
    try:
        if os.path.isfile(key) and os.path.getmtime(key) >= st.st_mtime:
            with open(key, "rb") as f:
                cand = f.read()
            # 解压后与源文件逐字节比对，确认不是陈旧缓存（3.2MB，耗时约 10ms）
            if _gz_matches_source(cand):
                blob = cand
    except OSError:
        blob = None
    if blob is None:
        try:
            with open(CONTENT_PATH, "rb") as f:
                blob = gzip.compress(f.read(), 6)
            try:
                with open(key, "wb") as f:
                    f.write(blob)
            except OSError:
                pass
        except OSError:
            return None
    cache.update({"mtime": st.st_mtime, "size": len(blob), "blob": blob})
    return blob


def _gz_matches_source(cand):
    """校验预生成的 .gz 是否与当前 content.json 内容一致（防陈旧缓存）。"""
    try:
        if not cand or cand[:2] != b"\x1f\x8b":
            return False
        with open(CONTENT_PATH, "rb") as f:
            src = f.read()
        return gzip.decompress(cand) == src
    except Exception:
        return False

# 题目 id → 学科/册/单元 映射（仪表盘聚合用；从 content.json 惰性加载并缓存）
_QMAP = {"loaded": False, "map": {}}

def qmap():
    if not _QMAP["loaded"]:
        _QMAP["loaded"] = True
        try:
            with open(CONTENT_PATH, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
            for q in data.get("questions") or []:
                _QMAP["map"][str(q.get("id"))] = {
                    "subject": q.get("subject") or "",
                    "volume": q.get("volume") or "",
                    "unit": q.get("unit") or "",
                }
            # 背诵卡记录 key 形如 subject@@unit@@front，按 @@ 取首段即可得学科，无需映射
        except Exception:
            pass
    return _QMAP["map"]

SESSION_HOURS = 12 * 7          # 登录态有效期（7 天）
PBKDF2_ROUNDS = 120_000
USER_RE = re.compile(r"^[\w\u4e00-\u9fa5.@\-]{2,20}$")


# ============================================================ 配置
DEFAULT_CONF = {
    "turso_url": "https://wc-game-ku-521.aws-ap-northeast-1.turso.io",
    "turso_token": "",
    "admin_key": "",            # 首次启动自动生成，登录 /admin 用
    "secret": "",               # 会话签名密钥，首次启动自动生成，切勿外泄
    "port": 8080,
    "price": "9.9",
    "wx_name": "鑫辰",
    "wx_area": "广东·佛山",
}


# 弱管理员口令黑名单：命中或长度过短则启动时强制重生成强随机串
_WEAK_KEYS = {"", "admin", "123456", "12345678", "password", "admin123", "2026123XC", "2026123xc"}
_ADMIN_KEY_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # 去除易混淆 0/O/1/l/I


def _strong_admin_key():
    return "".join(secrets.choice(_ADMIN_KEY_ALPHA) for _ in range(16))


def load_conf():
    conf = dict(DEFAULT_CONF)
    changed = False
    if os.path.exists(CONF_PATH):
        try:
            with open(CONF_PATH, "r", encoding="utf-8") as f:
                conf.update(json.load(f))
        except Exception as e:
            print("[!] 配置文件读取失败，使用默认值：", e)
    for k in ("admin_key", "secret"):
        cur = conf.get(k)
        weak = (k == "admin_key") and (not cur or len(cur) < 12 or cur in _WEAK_KEYS)
        if not cur or weak:
            # 管理员密码：16 位强随机（无易混淆字符）；会话密钥：32 字节随机
            if k == "admin_key":
                conf[k] = _strong_admin_key()
                print("[*] 检测到管理员口令偏弱，已自动升级为 16 位强随机串（已写入 server_config.json）")
            else:
                conf[k] = secrets.token_hex(32)
            changed = True
    if changed:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(CONF_PATH, 0o600)
        except Exception:
            pass
    return conf


CONF = load_conf()
TURSO_URL = (CONF.get("turso_url") or "").rstrip("/")
TURSO_TOKEN = CONF.get("turso_token") or ""
ADMIN_KEY = CONF.get("admin_key") or ""
ADMIN_USER = CONF.get("admin_user") or "ku521"
SECRET = (CONF.get("secret") or "").encode("utf-8")


def _quote_ident(name):
    """把标识符（表名/列名）安全地转成 SQL 字面量，避免拼接注入。"""
    return '"' + str(name).replace('"', '""') + '"'


def _safe_ident(name):
    """标识符白名单：只允许字母/数字/下划线且不以数字开头。"""
    s = str(name or "")
    return bool(s) and len(s) <= 64 and (s[0].isalpha() or s[0] == "_") and all(
        c.isalnum() or c == "_" for c in s)


# 后台数据浏览时需要遮蔽的凭据类字段（密码哈希 / 盐 / 口令）
_SECRET_COLS = ("pwd", "salt", "pw", "pass", "password", "token", "secret", "adminpw")

# 只读 SQL 白名单的补充：写操作关键字（词边界匹配，created_at 等普通列名不受影响）
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|vacuum|pragma|reindex)\b",
    re.I)


def _mask_secrets(rows):
    """对凭据类字段做脱敏，防止后台截图/转发时泄露密码哈希与盐。"""
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            kl = str(k).lower()
            if kl in _SECRET_COLS and v not in (None, ""):
                s = str(v)
                d[k] = (s[:6] + "…（已脱敏）") if len(s) > 6 else "…（已脱敏）"
            elif isinstance(v, str) and ("adminpw" in v.lower() or '"pw"' in v.lower()):
                # JSON 字符串里内嵌的口令字段（小游戏 kv 表等）
                v2 = re.sub(r'("(?:adminPw|pw|password)"\s*:\s*")[^"]*(")',
                             r'\1…\2', v, flags=re.I)
                d[k] = v2
            else:
                d[k] = v
        out.append(d)
    return out

PRICE = str(CONF.get("price") or "9.9")
WX_NAME = CONF.get("wx_name") or ""
WX_AREA = CONF.get("wx_area") or ""


# ============================================================ Turso（libSQL HTTP pipeline）
class TursoError(Exception):
    pass


def _cell(v):
    return {"type": "text", "value": str(v)}


def _cell_i(v):
    return {"type": "integer", "value": str(int(v))}


def turso(sql, args=None, retries=2):
    """执行一条 SQL，返回 list[dict]。args 为 [(值, 'text'|'int'), ...]"""
    return turso_multi([(sql, args)], retries=retries)[0]


def turso_multi(items, retries=2):
    """批量执行多条 SQL（一次 HTTP 往返），返回 [list[dict], ...]，顺序与入参一致。

    性能：Turso 是云端 HTTPS 库，单次往返实测 0.9~1.8 秒。
    启动阶段原本要串行/并发打 5~6 次库（/api/me、/api/rec/all、/api/dl/status、/api/dash…），
    累计 3~6 秒。Turso 的 pipeline 支持一次请求内执行多条语句，
    实测 3 条批量 876ms（vs 3×1200ms），因此把启动所需的查询合并成一次往返。
    """
    if not TURSO_URL or not TURSO_TOKEN:
        raise TursoError("服务器未配置 Turso 令牌，请检查 server_config.json")
    if not items:
        return []
    reqs = []
    for sql, args in items:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = [_cell_i(a[0]) if (len(a) > 1 and a[1] == "int") else _cell(a[0]) for a in args]
        reqs.append({"type": "execute", "stmt": stmt})
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                TURSO_URL + "/v2/pipeline",
                data=body,
                headers={
                    "Authorization": "Bearer " + TURSO_TOKEN,
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8"))
            res = j.get("results") or []
            for x in res:
                if x.get("type") == "error":
                    msg = ((x.get("error") or {}).get("message")) or "unknown"
                    raise TursoError(msg)
            out = []
            for i in range(len(items)):
                raw = (res[i] or {}).get("response", {}).get("result", {}) if i < len(res) else {}
                cols = [c.get("name") for c in (raw.get("cols") or [])]
                rows = []
                for row in (raw.get("rows") or []):
                    rows.append({cols[k]: (row[k] or {}).get("value") for k in range(len(cols))})
                out.append(rows)
            # 写 zb_users / zb_dl 会改变会员/审核/下载权益 → 立即失效对应缓存
            # （保证管理员改权限后马上生效，不必等 TTL 到期）
            for sql, _ in items:
                _s = sql.lstrip()[:60].upper()
                if _s.startswith(("UPDATE", "INSERT", "DELETE")):
                    try:
                        if "ZB_USERS" in _s:
                            with _USER_CACHE["lock"]:
                                _USER_CACHE["d"].clear()
                        # zb_dl / zb_dl_req / zb_dl_codes 里的表名后可能跟空格或左括号
                        if "ZB_DL" in _s and "ZB_DL_REQ" not in _s and "ZB_DL_CODES" not in _s:
                            with _DL_CACHE["lock"]:
                                _DL_CACHE["d"].clear()
                    except Exception:
                        pass
            return out
        except TursoError:
            raise
        except Exception as e:          # 网络抖动重试一次
            last_err = e
            if attempt < retries:
                time.sleep(0.6)
    raise TursoError("数据库连接失败：%s" % last_err)


TBL = """
CREATE TABLE IF NOT EXISTS zb_users (
  username   TEXT PRIMARY KEY,
  salt       TEXT NOT NULL,
  pwd        TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  member     INTEGER NOT NULL DEFAULT 0,
  expire     TEXT,
  phone      TEXT,
  note       TEXT,
  created_at TEXT,
  approved_at TEXT,
  last_login TEXT
)
"""


def db_init():
    try:
        turso(TBL)
        turso("CREATE TABLE IF NOT EXISTS zb_records ("
              "username TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
              "val TEXT, updated TEXT, PRIMARY KEY (username, kind, key))")
        turso("CREATE TABLE IF NOT EXISTS zb_checkin ("
              "username TEXT NOT NULL, day TEXT NOT NULL, gained INTEGER DEFAULT 0, "
              "ts TEXT, PRIMARY KEY (username, day))")
        # 下载权益：打赏解锁 / 兑换码
        turso("CREATE TABLE IF NOT EXISTS zb_dl ("
              "username TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'admin', granted_at TEXT)")
        turso("CREATE TABLE IF NOT EXISTS zb_dl_req ("
              "username TEXT PRIMARY KEY, code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
              "created_at TEXT, updated_at TEXT)")
        turso("CREATE TABLE IF NOT EXISTS zb_dl_codes ("
              "code TEXT PRIMARY KEY, created_at TEXT, used_by TEXT, used_at TEXT)")
        # 邀请裂变：邀请关系 + 激活码奖励（首次升级时补列）
        try:
            turso("ALTER TABLE zb_users ADD COLUMN invited_by TEXT")
        except Exception:
            pass
        # 会员两档：single=单年级版（绑定一个年级） / all=全三册版（首次升级时补列）
        try:
            turso("ALTER TABLE zb_users ADD COLUMN member_tier TEXT")
        except Exception:
            pass
        try:
            turso("ALTER TABLE zb_users ADD COLUMN member_grade TEXT")
        except Exception:
            pass
        try:
            turso("ALTER TABLE zb_users ADD COLUMN member_grades TEXT")
        except Exception:
            pass
        turso("CREATE TABLE IF NOT EXISTS zb_invite_reward ("
              "username TEXT PRIMARY KEY, code TEXT NOT NULL, claimed_at TEXT)")
        # 邀请裂变 · 防刷与有效性追踪
        # zb_invites：邀请关系明细（谁邀了谁、何时、是否有效、为何无效）
        turso("CREATE TABLE IF NOT EXISTS zb_invites ("
              "invitee TEXT PRIMARY KEY, inviter TEXT NOT NULL, "
              "created_at TEXT, ip TEXT, fp TEXT, ua TEXT, "
              "activated INTEGER DEFAULT 0, acted INTEGER DEFAULT 0, paid INTEGER DEFAULT 0, "
              "valid INTEGER DEFAULT 0, reason TEXT)")
        # zb_fprints：设备指纹表（同一设备注册的账号数用于拦截批量养号）
        turso("CREATE TABLE IF NOT EXISTS zb_fprints ("
              "fp TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0, "
              "first_ip TEXT, last_ip TEXT, updated_at TEXT)")
        # zb_invite_grants：阶梯奖励发放记录（保证同一档不重复发放）
        turso("CREATE TABLE IF NOT EXISTS zb_invite_grants ("
              "username TEXT NOT NULL, tier TEXT NOT NULL, granted_at TEXT, note TEXT, "
              "PRIMARY KEY (username, tier))")
        # zb_regmeta：账号注册时的 IP / 设备指纹（用于"邀请双方是否同源"判定）
        turso("CREATE TABLE IF NOT EXISTS zb_regmeta ("
              "username TEXT PRIMARY KEY, ip TEXT, fp TEXT, ua TEXT, created_at TEXT)")
        # zb_ipregs：IP 注册记录（同 IP 每日注册上限）
        turso("CREATE TABLE IF NOT EXISTS zb_ipregs ("
              "ip TEXT, day TEXT, count INTEGER NOT NULL DEFAULT 0, "
              "PRIMARY KEY (ip, day))")

        # 长期登录令牌（"记住我"）：只存令牌哈希，不存明文；令牌本身放在 HttpOnly Cookie 里
        turso("CREATE TABLE IF NOT EXISTS zb_sessions ("
              "token TEXT PRIMARY KEY, username TEXT NOT NULL, "
              "created_at TEXT, expires_at TEXT, last_used TEXT)")
        # 好友系统：好友关系 + 私信 + 刷新挑战
        turso("CREATE TABLE IF NOT EXISTS zb_friends ("
              "a TEXT NOT NULL, b TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
              "ts TEXT, PRIMARY KEY (a, b))")
        turso("CREATE TABLE IF NOT EXISTS zb_msgs ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, frm TEXT NOT NULL, to_u TEXT NOT NULL, "
              "text TEXT NOT NULL, ts TEXT, read INTEGER DEFAULT 0)")
        turso("CREATE TABLE IF NOT EXISTS zb_challenges ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, frm TEXT NOT NULL, to_u TEXT NOT NULL, "
              "status TEXT NOT NULL DEFAULT 'pending', frm_score INTEGER, to_score INTEGER, ts TEXT)")
        print("[OK] 数据表就绪")
    except Exception as e:
        print("[!] 建表失败：", e)


def migrate_member_grades():
    """一次性数据迁移（幂等，可重复执行）：
    旧会员行可能只有 member_tier/member_grade 而缺 member_grades，
    这里按真实档位回填年级列表——后台按年级显示的就是真实状态，
    不会出现"没写 grades 就按全三册算"从而默认全开。"""
    try:
        rows = turso("SELECT username, member_tier, member_grade, member_grades FROM zb_users WHERE member=1")
    except Exception:
        return
    n = 0
    for r in rows:
        if parse_grades(r.get("member_grades") or ""):
            continue
        tier = (r.get("member_tier") or "").strip()
        grade = (r.get("member_grade") or "").strip()
        gs = [grade] if (tier == "single" and grade in VALID_GRADES) else list(VALID_GRADES)
        try:
            turso("UPDATE zb_users SET member_grades=? WHERE username=?",
                  [(",".join(gs), "text"), (r["username"], "text")])
            n += 1
        except Exception:
            pass
    if n:
        print("[OK] 已回填 %d 个旧会员的年级列表" % n)


def hash_pwd(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS).hex()


_USER_CACHE = {"d": {}, "lock": __import__("threading").Lock()}


def get_user(u):
    """查用户（带短 TTL 内存缓存）。

    性能：Turso 是云端 HTTPS 库，单次往返实测 0.9~1.8 秒。
    前端登录流程串行调用 /api/health → /api/me → /api/content/ticket → /api/content，
    其中每次鉴权都要 get_user()，累计 3~5 秒 —— 这才是页面"卡"的真正原因。
    用户资料（会员状态、审核状态等）变动极少，故缓存 30 秒：
    正常访问序列 4 次 DB 往返 → 最多 1 次；管理员改会员后最多 30 秒生效（可用 invalidate_user 立即失效）。
    """
    now = time.time()
    with _USER_CACHE["lock"]:
        hit = _USER_CACHE["d"].get(u)
        if hit and now - hit[0] < 30:
            return hit[1]
    rows = turso("SELECT * FROM zb_users WHERE username = ?", [(u, "text")])
    row = rows[0] if rows else None
    with _USER_CACHE["lock"]:
        if len(_USER_CACHE["d"]) > 500:          # 防止长期运行内存膨胀
            _USER_CACHE["d"].clear()
        _USER_CACHE["d"][u] = (now, row)
    return row


def invalidate_user(u):
    """会员状态/审核状态变更后立即清缓存（管理员改权限时调用）。"""
    with _USER_CACHE["lock"]:
        _USER_CACHE["d"].pop(u, None)


def member_active(row):
    if not row or str(row.get("member")) not in ("1", "true", "True"):
        return False
    exp = (row.get("expire") or "").strip()
    if not exp or exp.lower() in ("forever", "永久"):
        return True
    try:
        return exp >= time.strftime("%Y-%m-%d")
    except Exception:
        return True


VALID_GRADES = ("高一", "高二", "高三")


def parse_grades(raw):
    out = []
    for x in (raw or "").split(","):
        x = x.strip()
        if x in VALID_GRADES and x not in out:
            out.append(x)
    return out


def member_info(row):
    """会员档位信息：grades=已开通年级列表（按年级开关制）。
    新数据以 member_grades 逗号串为准；旧数据（member_tier/member_grade）向后兼容：
    'all'→全三册，'single'→[grade]。**找不到任何年级依据时返回空（不默认全开）**，
    避免"没写年级就当成全三册"导致所有用户默认全开。"""
    if not member_active(row):
        return {"tier": "", "grade": "", "grades": []}
    grades = parse_grades(row.get("member_grades") or "")
    if grades:
        tier = "all" if len(grades) == 3 else "single"
        return {"tier": tier, "grade": grades[0] if tier == "single" else "", "grades": grades}
    tier = (row.get("member_tier") or "").strip()
    grade = (row.get("member_grade") or "").strip()
    if tier == "all":
        return {"tier": "all", "grade": "", "grades": list(VALID_GRADES)}
    if tier == "single" and grade in VALID_GRADES:
        return {"tier": "single", "grade": grade, "grades": [grade]}
    return {"tier": "", "grade": "", "grades": []}


_DL_CACHE = {"d": {}, "lock": __import__("threading").Lock()}


def has_dl(username):
    """下载权益是否已开通（带 30s 缓存）。

    /api/me 每次都要问一次下载权益，是启动序列里剩下的最后 1 次 DB 往返（约 0.9~1.8 秒）。
    下载权限变动极少，缓存 30 秒足够；开通/关闭时用 invalidate_dl() 立即失效。
    """
    now = time.time()
    with _DL_CACHE["lock"]:
        hit = _DL_CACHE["d"].get(username)
        if hit is not None and now - hit[0] < 30:
            return hit[1]
    try:
        ok = bool(turso("SELECT username FROM zb_dl WHERE username=?", [(username, "text")]))
    except Exception:
        ok = False
    with _DL_CACHE["lock"]:
        if len(_DL_CACHE["d"]) > 500:
            _DL_CACHE["d"].clear()
        _DL_CACHE["d"][username] = (now, ok)
    return ok


def invalidate_dl(username):
    with _DL_CACHE["lock"]:
        _DL_CACHE["d"].pop(username, None)


def esc_html(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 下载中心：分科 docx 文件（存于 site/files/dl/）
DL_FILES = [
    {"id": "pol",   "file": "01_政治精华总结.docx",          "name": "政治精华总结（打印版）",   "subject": "政治",
     "desc": "政治各册精华：知识点梳理 + 赋分术语 + 必背卡片，打印即背。"},
    {"id": "hist",  "file": "02_历史精华总结.docx",          "name": "历史精华总结（打印版）",   "subject": "历史",
     "desc": "历史时间线 + 阶段特征 + 大题答法模板，按册拆分。"},
    {"id": "geo",   "file": "03_地理精华总结.docx",          "name": "地理精华总结（打印版）",   "subject": "地理",
     "desc": "地理核心考点 + 区位分析 + 图文导图，自然/人文全覆盖。"},
    {"id": "bio",   "file": "05_生物精华总结.docx",          "name": "生物精华总结（打印版）",   "subject": "生物",
     "desc": "生物必修 1~3 概念网络 + 实验要点 + 易错辨析。"},
    {"id": "guide", "file": "04_资料使用指南与试题索引.docx", "name": "资料使用指南与试题索引",   "subject": "综合",
     "desc": "怎么学、怎么背、试题索引与对应章节，新手必读。"},
    {"id": "pack",  "file": "高一上册学习包(打印版).docx",    "name": "高一上册全科学习包",       "subject": "综合",
     "desc": "高一上册四科合一打包，省去逐个下载。"},
    {"id": "geo_mock1", "file": "mock_地理_1.docx", "name": "地理高考模拟卷（一）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "geo_mock2", "file": "mock_地理_2.docx", "name": "地理高考模拟卷（二）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "geo_mock3", "file": "mock_地理_3.docx", "name": "地理高考模拟卷（三）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "geo_mock4", "file": "mock_地理_4.docx", "name": "地理高考模拟卷（四）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "geo_mock5", "file": "mock_地理_5.docx", "name": "地理高考模拟卷（五）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "geo_mock6", "file": "mock_地理_6.docx", "name": "地理高考模拟卷（六）", "subject": "地理",
     "desc": "地理高考选择题模拟套卷，25题/卷，含参考答案，按统编人教版考点命制，用于自测与查漏。"},
    {"id": "bio_mock1", "file": "mock_生物_1.docx", "name": "生物高考模拟卷（一）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "bio_mock2", "file": "mock_生物_2.docx", "name": "生物高考模拟卷（二）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "bio_mock3", "file": "mock_生物_3.docx", "name": "生物高考模拟卷（三）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "bio_mock4", "file": "mock_生物_4.docx", "name": "生物高考模拟卷（四）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "bio_mock5", "file": "mock_生物_5.docx", "name": "生物高考模拟卷（五）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "bio_mock6", "file": "mock_生物_6.docx", "name": "生物高考模拟卷（六）", "subject": "生物",
     "desc": "生物高考选择题模拟套卷，25题/卷，含参考答案，覆盖必修与选择性必修核心考点。"},
    {"id": "hist_r1", "file": "历史-2025陕晋宁青高考历史试题（解析）.docx", "name": "2025陕晋宁青高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r2", "file": "历史-2025江西高考历史试题（解析）.docx", "name": "2025江西高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r3", "file": "历史-2026广西高考历史试题（解析）.docx", "name": "2026广西高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r4", "file": "历史-2026湖南高考历史试题（解析）.docx", "name": "2026湖南高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r5", "file": "历史-2026四川高考历史试题（解析）.docx", "name": "2026四川高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r6", "file": "历史-2026云南高考历史试题（解析）.docx", "name": "2026云南高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r7", "file": "历史-2026河北高考历史试题（解析）.docx", "name": "2026河北高考历史试题（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r8", "file": "历史-2025山东潍坊二模（解析）.docx", "name": "2025山东潍坊二模（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r9", "file": "历史-2025年12月版（解析）.docx", "name": "2025年12月版（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r10", "file": "历史-2025年7月版（解析）.docx", "name": "2025年7月版（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r11", "file": "历史-2025年9月版（解析）.docx", "name": "2025年9月版（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_r12", "file": "历史-2025年5月版（解析）.docx", "name": "2025年5月版（解析版）", "subject": "历史",
     "desc": "历史真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r13", "file": "政治-2025届武汉市华中师范大学第一附属中学高三高考前政治试题（解析）.docx", "name": "2025届武汉市华中师范大学第一附属中学高三高考前政治试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r14", "file": "政治-2025届山东省临沂某校高三上学期第一次“小高考”模拟考试政治试题（解析）.docx", "name": "2025届山东省临沂某校高三上学期第一次“小高考”模拟考试政治试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r15", "file": "政治-2025江苏高中政治真题（解析）.docx", "name": "2025江苏高中政治真题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r16", "file": "政治-2025山东高中政治真题（解析）.docx", "name": "2025山东高中政治真题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r17", "file": "政治-2026届山东省潍坊三模政治试题（解析）.docx", "name": "2026届山东省潍坊三模政治试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r18", "file": "政治-2026届江苏省苏北七市高三下学期二模政治试题（解析）.docx", "name": "2026届江苏省苏北七市高三下学期二模政治试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r19", "file": "政治-2026届安徽省合肥市第一中学高三1月月考试题（解析）.docx", "name": "2026届安徽省合肥市第一中学高三1月月考试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r20", "file": "政治-2026届浙江宁波市镇海中学高三下学期模拟预测政治试题（解析）.docx", "name": "2026届浙江宁波市镇海中学高三下学期模拟预测政治试题（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r21", "file": "政治-2025年10月版（解析）.docx", "name": "2025年10月版（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r22", "file": "政治-2025年11月版（解析）.docx", "name": "2025年11月版（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r23", "file": "政治-2025年12月版（解析）.docx", "name": "2025年12月版（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "pol_r24", "file": "政治-2025年9月版（解析）.docx", "name": "2025年9月版（解析版）", "subject": "政治",
     "desc": "政治真实试卷（含详细解析），来源：智慧中小学/各地模拟与高考真题整理。"},
    {"id": "hist_g2_1", "file": "mock_历史_高二纲要下_1.docx", "name": "高二历史·世界史模块模拟卷（一）", "subject": "历史",
     "desc": "高二《中外历史纲要·下》世界史模块同步模拟，25题/卷，含参考答案，覆盖新航路、资产阶级革命、工业革命、十月革命、二战与冷战等核心考点。"},
    {"id": "hist_g2_2", "file": "mock_历史_高二纲要下_2.docx", "name": "高二历史·世界史模块模拟卷（二）", "subject": "历史",
     "desc": "高二世界史同步模拟卷之二，25题/卷，含参考答案，用于高二阶段自测与查漏。"},
    {"id": "hist_g2_3", "file": "mock_历史_高二纲要下_3.docx", "name": "高二历史·世界史模块模拟卷（三）", "subject": "历史",
     "desc": "高二世界史同步模拟卷之三，25题/卷，含参考答案。"},
    {"id": "hist_g2_4", "file": "mock_历史_高二纲要下_4.docx", "name": "高二历史·世界史模块模拟卷（四）", "subject": "历史",
     "desc": "高二世界史同步模拟卷之四，25题/卷，含参考答案。"},
    {"id": "geo_gk_1", "file": "mock_地理_高考综合_1.docx", "name": "地理高考综合冲刺模拟卷（一）", "subject": "地理",
     "desc": "地理高考综合冲刺套卷，25题/卷，含参考答案，覆盖自然、人文与区域地理综合考点，适合高三刷题自测。"},
    {"id": "geo_gk_2", "file": "mock_地理_高考综合_2.docx", "name": "地理高考综合冲刺模拟卷（二）", "subject": "地理",
     "desc": "地理高考综合冲刺套卷之二，25题/卷，含参考答案。"},
    {"id": "geo_gk_3", "file": "mock_地理_高考综合_3.docx", "name": "地理高考综合冲刺模拟卷（三）", "subject": "地理",
     "desc": "地理高考综合冲刺套卷之三，25题/卷，含参考答案。"},
    {"id": "geo_gk_4", "file": "mock_地理_高考综合_4.docx", "name": "地理高考综合冲刺模拟卷（四）", "subject": "地理",
     "desc": "地理高考综合冲刺套卷之四，25题/卷，含参考答案。"},
    {"id": "bio_gk_1", "file": "mock_生物_高考综合_1.docx", "name": "生物高考综合冲刺模拟卷（一）", "subject": "生物",
     "desc": "生物高考综合冲刺套卷，25题/卷，含参考答案，覆盖稳态与环境、遗传与进化、生物技术等核心考点，适合高三刷题自测。"},
    {"id": "bio_gk_2", "file": "mock_生物_高考综合_2.docx", "name": "生物高考综合冲刺模拟卷（二）", "subject": "生物",
     "desc": "生物高考综合冲刺套卷之二，25题/卷，含参考答案。"},
    {"id": "bio_gk_3", "file": "mock_生物_高考综合_3.docx", "name": "生物高考综合冲刺模拟卷（三）", "subject": "生物",
     "desc": "生物高考综合冲刺套卷之三，25题/卷，含参考答案。"},
    {"id": "bio_gk_4", "file": "mock_生物_高考综合_4.docx", "name": "生物高考综合冲刺模拟卷（四）", "subject": "生物",
     "desc": "生物高考综合冲刺套卷之四，25题/卷，含参考答案。"},
    # 后续把“测试试卷原文档”放进来即可：在 _dl_dir() 目录放入 docx，并在此处加一行（desc 写清楚内容）。
]

# 学科精华打印版样式（浏览器打印/另存为 PDF）
PRINT_CSS = """
*{box-sizing:border-box}
body{margin:0 auto;max-width:840px;padding:26px 20px 70px;font-family:"Microsoft YaHei","PingFang SC",sans-serif;font-size:11.5pt;line-height:1.7;color:#1d2733;background:#fff}
h1.cover{font-size:20pt;text-align:center;margin:18px 0 4px}
.meta{text-align:center;color:#6b7785;font-size:10pt;margin-bottom:22px}
h2{font-size:15pt;border-bottom:2px solid #2a5db0;color:#2a5db0;padding-bottom:4px;margin:30px 0 10px;page-break-after:avoid}
h3{font-size:12.5pt;color:#1a3a6b;margin:20px 0 8px;page-break-after:avoid}
.sec{margin:6px 0}
.mm{overflow:visible}
table{border-collapse:collapse;width:100%;font-size:9.5pt}
th,td{border:1px solid #b9c6d8;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#eef4fd}
.kcard{border:1px solid #d6dfec;border-radius:8px;padding:8px 11px;margin:8px 0;page-break-inside:avoid}
.kcard .fr{font-weight:700}
.kcard .bk{margin-top:4px;color:#33405a;white-space:pre-wrap}
.kcard .star{color:#f5a623}
.toolbar{position:fixed;top:12px;right:14px;background:#fff;border:1px solid #d6dfec;border-radius:10px;padding:10px 14px;box-shadow:0 4px 16px rgba(20,50,90,.15);z-index:9}
.toolbar button{background:#1565c0;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer;font-family:inherit;font-weight:700}
.toolbar span{display:block;font-size:11px;color:#8a94a3;margin-top:6px;max-width:220px}
@media print{.toolbar{display:none}body{padding:0;max-width:none}.kcard{page-break-inside:avoid}}
@page{size:A4;margin:14mm 13mm}
"""


def _mask_name(u):
    """用户名脱敏（加好友后可见全名）。规则：
    - 7~11位：保留前3位+后2位，中间藏 n-5 个（7位藏2个，每多1位多藏1个）
    - 超过11位：统一只藏中间6个（前后仍留3+2，不再随长度加长）
      —— 这样超长名与11位名的显示宽度一致，也不会泄露真实用户名长度
    - 6位：隐藏中间2个（前2+后2）
    - 5位及以下：隐藏中间1个
    """
    u = (u or "").strip()
    n = len(u)
    if n <= 1:
        return u or "*"
    if n == 2:
        return u[0] + "*"
    if n <= 5:                      # 藏中间1个：ab*de / ab*cd / a*c / a*
        front = (n - 1) // 2
        back = n - 1 - front
        return u[:front] + "*" + (u[n - back:] if back else "")
    if n == 6:                      # 藏中间2个：ab**ef
        return u[:2] + "**" + u[-2:]
    # ≥7：前3+后2；藏中间最多6个（>11位统一藏6个）
    return u[:3] + "*" * min(n - 5, 6) + u[-2:]


# ============================================================ 会话 / 签名
def _sign(data: bytes) -> str:
    return hmac.new(SECRET, data, hashlib.sha256).hexdigest()


def make_sid(username: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "e": int(time.time()) + SESSION_HOURS * 3600}).encode()
    ).decode()
    return payload + "." + _sign(payload.encode())[:40]


# ===== 长期登录令牌（"记住我"） =====
# 安全设计：
#   · 令牌本身是 32 字节随机串（secrets.token_hex），只以 SHA-256 哈希形式落库；
#   · 令牌放在 HttpOnly Cookie（zb_rt）里 —— JS 读不到，XSS 也偷不走；
#   · localStorage 只保存用户名（公开信息），绝不保存密码；
#   · 支持服务端撤销（登出 / 改密码即失效全部长期令牌）。
REFRESH_DAYS = 180
REFRESH_COOKIE = "zb_rt"


def _tok_hash(tok):
    return hashlib.sha256(("zr|" + tok).encode()).hexdigest()


def issue_refresh_token(username):
    """签发长期令牌，返回 (明文令牌, 过期时间戳)。"""
    tok = secrets.token_hex(32)
    now = time.time()
    exp = now + REFRESH_DAYS * 86400
    turso("INSERT INTO zb_sessions (token,username,created_at,expires_at,last_used) VALUES (?,?,?,?,?)",
          [(_tok_hash(tok), "text"), (username, "text"),
           (time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "text"),
           (time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)), "text"),
           (time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "text")])
    return tok, exp


def verify_refresh_token(tok):
    """校验长期令牌，返回用户名或 None（过期则清理）。"""
    if not tok:
        return None
    rows = turso("SELECT username,expires_at FROM zb_sessions WHERE token=?", [(_tok_hash(tok), "text")])
    if not rows:
        return None
    exp = rows[0].get("expires_at") or ""
    if exp and exp < time.strftime("%Y-%m-%d %H:%M"):
        turso("DELETE FROM zb_sessions WHERE token=?", [(_tok_hash(tok), "text")])
        return None
    # 滑动续期：剩余不足 30 天则自动延长
    try:
        remain = time.mktime(time.strptime(exp, "%Y-%m-%d %H:%M")) - time.time()
        if remain < 30 * 86400:
            newexp = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + REFRESH_DAYS * 86400))
            turso("UPDATE zb_sessions SET expires_at=?, last_used=? WHERE token=?",
                  [(newexp, "text"), (time.strftime("%Y-%m-%d %H:%M"), "text"), (_tok_hash(tok), "text")])
    except Exception:
        pass
    return rows[0].get("username")


def revoke_refresh_tokens(username):
    """撤销某用户的全部长期令牌（改密码 / 登出时调用）。"""
    try:
        turso("DELETE FROM zb_sessions WHERE username=?", [(username, "text")])
    except Exception:
        pass


def read_sid(value: str):
    try:
        payload, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode())[:40]):
            return None
        d = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if int(d.get("e", 0)) < time.time():
            return None
        return d.get("u")
    except Exception:
        return None


def admin_sig() -> str:
    return _sign(b"admin|" + ADMIN_KEY.encode())[:40]


# ============================================================ 登录限流（内存）
_FAILS = {}
_LOCK = __import__("threading").Lock()


def too_many(ip: str) -> bool:
    now = time.time()
    with _LOCK:
        arr = [t for t in _FAILS.get(ip, []) if now - t < 600]
        _FAILS[ip] = arr
        return len(arr) >= 8


def mark_fail(ip: str):
    with _LOCK:
        _FAILS.setdefault(ip, []).append(time.time())


def clear_fail(ip: str):
    with _LOCK:
        _FAILS.pop(ip, None)


# ============================================================ 内容防爬 / 后台审计
# 内容凭证：登录用户先取一次性、短时效凭证，再拉 /api/content；凭证参与 HMAC 签名，
# 不可伪造、不可重放，从根本上抬高“一键爬完题库/知识点/卡片”的成本。
_CONTENT_TOKENS = {}     # nonce -> 过期时间戳(绝对)
_CONTENT_HITS = {}       # username -> [拉取时间戳]
_REG_HITS = {}           # ip -> [注册时间戳]


def issue_content_ticket(u: str) -> str:
    now = time.time()
    for k in [x for x, v in _CONTENT_TOKENS.items() if v < now]:
        _CONTENT_TOKENS.pop(k, None)
    n = secrets.token_hex(16)
    _CONTENT_TOKENS[n] = now + 120          # 120 秒有效
    return n + "." + _sign(("ct|" + u + "|" + n).encode())


def consume_content_ticket(u: str, tok: str) -> bool:
    if not tok or "." not in tok:
        return False
    n, sig = tok.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(("ct|" + u + "|" + n).encode())):
        return False
    exp = _CONTENT_TOKENS.pop(n, None)      # 单次使用：消费即作废
    return exp is not None and exp >= time.time()


def content_too_many(u: str) -> bool:
    now = time.time()
    with _LOCK:
        arr = [t for t in _CONTENT_HITS.get(u, []) if now - t < 3600]
        _CONTENT_HITS[u] = arr
        # 单账号每小时最多 60 次内容拉取。
        # 原为 20 次，但前端每次页面加载都会拉一次资料；叠加失败重试后，
        # 20 次配额会在几分钟内被耗尽，导致后续正常访问也返回 429 —— 自锁死。
        return len(arr) >= 60


def content_mark(u: str):
    with _LOCK:
        _CONTENT_HITS.setdefault(u, []).append(time.time())


# ============================================================ 邀请裂变 · 防刷
# 设计目标：防止"自己批量注册小号刷邀请奖励"。
# 三道防线：
#   ① IP 限制   —— 同 IP 每日注册上限（默认 3 个），防同一网络批量开号；
#   ② 设备指纹  —— 同设备（同浏览器）注册上限（默认 2 个），防换 IP 批量开号；
#   ③ 邀请关系  —— 被邀请人与邀请人不得同 IP / 同设备；且不能自我邀请。
# 另外：被邀请人必须产生"使用痕迹"（激活 + 学习行为，且过半有充值）才计入有效邀请，
#       僵尸号（注册后不登录/不学习/不付费）不计数。
# 阈值权衡：
#   · 同 IP 可能是一整个宿舍/教室共用出口（校园网、家里宽带），
#     限得太紧会误伤真实同学 → 放宽到 5 个/天；
#   · 同设备（浏览器指纹）几乎必然是同一个人批量开号 → 严格限制 2 个。
REG_MAX_PER_IP_DAY = 5        # 同 IP 每日最多注册
REG_MAX_PER_FP = 2            # 同设备最多注册
REG_BLOCK_SAME_IP_INVITE = True  # 邀请双方不得同 IP
REG_BLOCK_SAME_FP_INVITE = True  # 邀请双方不得同设备


def device_fp(ua: str, accept_lang: str = "") -> str:
    """生成设备指纹（服务端计算，不信任前端传值）。

    只用 User-Agent + 语言 + 平台特征做哈希，够用于识别"同一浏览器批量开号"，
    不做侵入式指纹采集。指纹变化（换浏览器/无痕）不影响正常使用。
    """
    raw = "%s|%s" % ((ua or "")[:400], (accept_lang or "")[:80])
    return hashlib.sha256(("fp|" + raw).encode()).hexdigest()[:32]


def reg_reached(ip: str, fp: str) -> str:
    """注册前检查：返回空字符串表示允许，否则返回拒绝原因。"""
    day = time.strftime("%Y-%m-%d")
    # 先判设备（同浏览器批量开号，最确定的刷号行为）
    if fp:
        try:
            rows = turso("SELECT count FROM zb_fprints WHERE fp=?", [(fp, "text")])
            n_fp = int(rows[0]["count"]) if rows else 0
            if n_fp >= REG_MAX_PER_FP:
                return "该浏览器注册账号过多（每个浏览器最多 %d 个账号，请用其他设备注册）" % REG_MAX_PER_FP
        except Exception:
            pass
    # 再判 IP（同网络可能是校园/家庭共享出口，阈值放宽）
    try:
        rows = turso("SELECT count FROM zb_ipregs WHERE ip=? AND day=?", [(ip or "?", "text"), (day, "text")])
        n_ip = int(rows[0]["count"]) if rows else 0
        if n_ip >= REG_MAX_PER_IP_DAY:
            return "同一网络注册过于频繁（同一网络每天最多 %d 个账号），请明天再试或让同学换个网络" % REG_MAX_PER_IP_DAY
    except Exception:
        pass
    return ""


def reg_mark_fp(ip: str, fp: str):
    """注册成功后记账（IP 日计数 + 设备指纹计数）。"""
    day = time.strftime("%Y-%m-%d")
    try:
        turso("INSERT INTO zb_ipregs (ip,day,count) VALUES (?,?,1) "
              "ON CONFLICT(ip,day) DO UPDATE SET count=count+1",
              [(ip or "?", "text"), (day, "text")])
    except Exception:
        pass
    if not fp:
        return
    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        turso("INSERT INTO zb_fprints (fp,count,first_ip,last_ip,updated_at) VALUES (?,1,?,?,?) "
              "ON CONFLICT(fp) DO UPDATE SET count=count+1, last_ip=excluded.last_ip, updated_at=excluded.updated_at",
              [(fp, "text"), (ip or "?", "text"), (ip or "?", "text"), (ts, "text")])
    except Exception:
        pass


def invite_self_block(inviter_row: dict, invitee: str, ip: str, fp: str) -> str:
    """校验邀请关系是否合规（自我邀请 / 同设备 / 同 IP）。返回空字符串表示允许。"""
    if not inviter_row:
        return ""
    inviter_name = inviter_row.get("username") or ""
    if inviter_name == invitee:
        return "不能邀请自己"
    # 查邀请人注册时的 IP / 设备（zb_regmeta，与"是否被邀请过"无关）
    try:
        rows = turso("SELECT ip,fp FROM zb_regmeta WHERE username=?", [(inviter_name, "text")])
        if rows:
            r = rows[0]
            if REG_BLOCK_SAME_FP_INVITE and fp and r.get("fp") and r.get("fp") == fp:
                return "不能在同一设备上邀请注册（防刷号）"
            # 同 IP 单独不拦（校园/家庭共享出口很常见），
            # 只有"同 IP 且同设备"才判定为同一人刷号（同设备规则已在上方覆盖）
            if (REG_BLOCK_SAME_IP_INVITE and ip and r.get("ip") and r.get("ip") == ip
                    and REG_BLOCK_SAME_FP_INVITE and fp and r.get("fp") and r.get("fp") == fp):
                return "不能在同一设备上邀请注册（防刷号）"
    except Exception:
        pass
    # 环检测：若该邀请人是被邀请人（或其邀请链上的人）邀请来的，说明是同伙刷号闭环
    try:
        # 直接互邀：invitee 已邀请过 inviter
        back = turso("SELECT 1 AS x FROM zb_invites WHERE inviter=? AND invitee=? LIMIT 1",
                     [(invitee, "text"), (inviter_name, "text")])
        if back:
            return "不能互相邀请（防刷号）"
        # 三人环：invitee -> X -> inviter
        mid = turso("SELECT invitee FROM zb_invites WHERE inviter=? LIMIT 20",
                    [(invitee, "text")])
        for m in mid:
            if m.get("invitee") == inviter_name:
                return "不能互相邀请（防刷号）"
    except Exception:
        pass
    return ""


# ============================================================ 未读数 · 版本号缓存
# 背景：Turso 单次往返 0.9~2.6 秒，而未读轮询每 8~45 秒就要查一次库，
#       多人在线时数据库很快成为瓶颈（实测 2 秒轮询会占满 78 秒/分钟的库时间）。
# 方案：服务端维护「每个用户的未读版本号 + 未读数缓存」——
#       · 有人发新消息给某用户 → 该用户版本号 +1（内存操作，零 DB）；
#       · 轮询时先看版本号：与上次相同 → 直接返回缓存的未读数（**不查库**）；
#         版本变了 → 才真正查一次库并刷新缓存。
# 效果：绝大多数轮询请求变成纯内存响应，数据库压力下降一个数量级。
_UNREAD_VER = {}      # {username: version}
_UNREAD_CACHE = {}    # {username: {"unread": {name: n}, "ver": v, "ts": ts}}
_UNREAD_LOCK = __import__("threading").Lock()
_FRIENDS_CACHE = {}     # {username: (ts, {"friends":[...], "incoming":[...], "outgoing":[...]})}


def unread_bump(username: str):
    """给某个用户的未读版本号 +1（有新消息到达时调用）。"""
    with _UNREAD_LOCK:
        _UNREAD_VER[username] = _UNREAD_VER.get(username, 0) + 1


def unread_version(username: str) -> int:
    with _UNREAD_LOCK:
        return _UNREAD_VER.get(username, 0)


def unread_get(username: str):
    """取该用户的未读数据（带缓存）。

    返回 (unread_dict, changed)。changed=False 表示版本未变、数据来自缓存（未查库）。
    """
    ver = unread_version(username)
    with _UNREAD_LOCK:
        c = _UNREAD_CACHE.get(username)
        if c and c.get("ver") == ver:
            return c["unread"], False
    # 版本变了（或首次）→ 真查一次库
    rows = turso("SELECT frm, COUNT(*) AS n FROM zb_msgs WHERE to_u=? AND read=0 GROUP BY frm",
                 [(username, "text")])
    unread = {r["frm"]: r["n"] for r in rows}
    with _UNREAD_LOCK:
        _UNREAD_CACHE[username] = {"unread": unread, "ver": ver, "ts": time.time()}
    return unread, True


def _friends_cache_drop(username: str):
    """社交关系变化后清掉好友列表缓存与未读缓存。"""
    try:
        _FRIENDS_CACHE.pop(username, None)
    except Exception:
        pass
    try:
        unread_drop(username)
    except Exception:
        pass


def unread_drop(username: str):
    """清掉某用户的缓存（读消息后调用，让下次轮询重新算）。"""
    with _UNREAD_LOCK:
        _UNREAD_CACHE.pop(username, None)
def reg_too_many(ip: str) -> bool:
    now = time.time()
    with _LOCK:
        arr = [t for t in _REG_HITS.get(ip, []) if now - t < 3600]
        _REG_HITS[ip] = arr
        return len(arr) >= 10               # 单 IP 每小时最多注册 10 个账号，防批量养号爬取


def reg_mark(ip: str):
    with _LOCK:
        _REG_HITS.setdefault(ip, []).append(time.time())


def _cors_origin_ok(origin):
    """仅允许可信源回显 CORS，杜绝任意网站借用户登录态读取接口。"""
    if not origin:
        return False
    try:
        from urllib.parse import urlparse
        h = urlparse(origin).netloc.lower()
    except Exception:
        return False
    if h in ("localhost", "127.0.0.1") or h.startswith("localhost:") or h.startswith("127.0.0.1:"):
        return True
    if h.endswith(".workbuddy.host"):       # 本站各环境的同源/子域
        return True
    return False


def admin_audit(action, user, ip, detail=""):
    try:
        with open(os.path.join(BASE, "admin_audit.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\t" + str(action) + "\t"
                    + str(user) + "\t" + str(ip) + "\t" + (detail or "") + "\n")
    except Exception:
        pass


def log_content(u, ip):
    try:
        with open(os.path.join(BASE, "content_access.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\t" + str(ip) + "\t" + str(u) + "\n")
    except Exception:
        pass


# ============================================================ 管理员页面
ADMIN_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>后台 · 会员审核</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;background:#f2f5fa;color:#1d2733}
.wrap{max-width:960px;margin:0 auto;padding:18px}
h1{font-size:19px;margin:6px 0 14px}
.card{background:#fff;border:1px solid #e3e9f2;border-radius:14px;padding:16px;box-shadow:0 2px 10px rgba(20,50,90,.05)}
input{width:100%;padding:11px 12px;border:1px solid #d7dfea;border-radius:10px;font-size:14px;margin-bottom:10px;font-family:inherit}
.btn{width:100%;padding:12px;border:0;border-radius:10px;background:#1565c0;color:#fff;font-size:15px;font-weight:700;cursor:pointer}
.btn:active{transform:translateY(1px)}
.btn.g{background:#0f9d58}.btn.r{background:#d93b3b}
.btn.s{width:auto;padding:7px 12px;font-size:12.5px;font-weight:600;border-radius:8px}
.msg{font-size:13px;color:#d93b3b;min-height:18px;margin:8px 0}
.msg.ok{color:#0f9d58}
.hide{display:none}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.stat{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.stat div{background:#eef4ff;border:1px solid #d8e4fb;border-radius:10px;padding:8px 12px;font-size:13px}
.stat b{font-size:16px;color:#1565c0}
.row{border:1px solid #e8eef7;border-radius:12px;padding:12px;margin-bottom:10px}
.row .u{font-weight:800;font-size:15px}
.row .m{font-size:12.5px;color:#6b7787;margin-top:3px;line-height:1.6}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;margin-left:6px;vertical-align:2px}
.t-p{background:#fff3e0;color:#b26a00}.t-a{background:#e8f5e9;color:#1b7a35}.t-r{background:#fdecea;color:#b3261e}
.t-v{background:#e3f2fd;color:#1565c0}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
.tip{font-size:12px;color:#8a94a3;margin-top:12px;line-height:1.7}
</style></head><body><div class="wrap">
<h1>政史地学习站 · 后台</h1>

<div id="loginBox" class="card">
<div style="font-size:13.5px;color:#5c6878;margin-bottom:10px">请输入管理员账号与密码（存于服务器，网页源码查不到）</div>
<input id="user" placeholder="管理员账号" autocomplete="username">
<input id="key" type="password" placeholder="管理员密码" autocomplete="current-password">
<button class="btn" id="go">进入后台</button>
  <div class="msg" id="lmsg"></div>
</div>

<div id="panel" class="hide">
  <div class="card" style="padding:12px;margin-bottom:12px">
    <div style="font-size:13px;color:#5c6878;margin-bottom:8px"><b>🔑 管理员密码</b>（存于服务器本地配置，手机打不开配置文件时在这里查看）</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id="admKey" type="text" readonly placeholder="点击「查看密码」加载" style="flex:1;min-width:180px;margin-bottom:0;font-family:monospace;letter-spacing:.5px">
      <button class="btn s" id="admKeyShow" style="margin-bottom:0">查看密码</button>
      <button class="btn s" id="admKeyCopy" style="margin-bottom:0">复制</button>
      <button class="btn s" id="admKeyReset" style="margin-bottom:0;background:#b3541e;color:#fff">重置密码</button>
    </div>
    <div class="tip" id="admKeyMsg"></div>
  </div>
  <div class="top">
    <div style="font-size:13px;color:#5c6878;line-height:1.75">待审核账号先点「通过」才能登录；<b style="color:#1565c0">点「高一 / 高二 / 高三」= 只开通该年级</b>（覆盖其它年级，不会误开成全三册）；若当前只开着该年级，再点一次 = 关闭；「全开三册」= 三册全通（¥16.9）；「取消会员」= 全部关闭。<b>新注册用户默认不开会员，只有浏览权限：不能做题、不能下载。</b></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <button class="btn s" id="home" style="background:#1565c0;color:#fff" title="关闭后台，返回学习首页（不退出登录）">← 返回主页</button>
      <button class="btn s" id="out" style="background:#6b7787" title="退出后台登录并返回主页">退出登录</button>
    </div>
  </div>
  <div class="stat" id="stat"></div>
  <div class="card" style="padding:12px">
    <div id="list" style="font-size:13px;color:#8a94a3">加载中…</div>
  </div>
  <div class="card" style="padding:12px;margin-top:12px">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px">
      <div style="font-weight:800;font-size:14px">📥 下载权益（打赏解锁 / 兑换码）</div>
      <button class="btn s" onclick="genCodes()">批量生成兑换码</button>
    </div>
    <div id="dlBox" style="font-size:13px;color:#8a94a3">加载中…</div>
    <div class="tip" style="margin-top:8px">· 学生端提交的申请带 4 位申请码，核对微信打赏截图后点「开通」，学生页面会自动解锁；<br>· 兑换码一码一用，学生输入即解锁，可通过微信单独发给已打赏的同学；<br>· 点已开通名单里的名字可撤销其下载权益。</div>
  </div>
  <div class="tip">· “通过”= 允许登录（通过后仍是<b>仅浏览</b>权限）；<br>· 点某个<b>年级</b>= 只开通该年级的答题/测试/错题/背诵/赋分 + 下载（¥9.9 档）；<br>· “全开”= 三个年级全部开通（¥16.9 全三册档）；<br>· “取消会员”= 收回全部权益，恢复为仅浏览；<br>· 删除后该账号立即失效，可重新注册。</div>
</div>
</div>
<script>
function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
async function api(p,body){
  const r = await fetch(p,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},credentials:'same-origin',body:body?JSON.stringify(body):undefined});
  let j={}; try{ j=await r.json() }catch(e){}
  if(!r.ok) throw new Error(j.msg||('HTTP '+r.status));
  return j;
}
// ===== 管理员密码：查看 / 复制 / 重置 =====
// 背景：admin_key 存在服务器本地 server_config.json，手机上打不开该文件，
// 忘记密码就无法登录后台。这里把"看本地配置"搬到网页上（需已登录后台）。
$('admKeyShow').onclick=async()=>{
  const m=$('admKeyMsg');
  m.textContent='读取中…';
  try{
    const d=await api('/api/admin/key');
    $('admKey').value=d.key||'';
    m.textContent='当前管理员账号：'+(d.user||'')+'　密码已显示，点「复制」可复制到剪贴板';
  }catch(e){ m.textContent='读取失败：'+e.message; }
};
$('admKeyCopy').onclick=async()=>{
  const m=$('admKeyMsg');
  const v=$('admKey').value;
  if(!v){ m.textContent='请先点「查看密码」'; return; }
  try{ await navigator.clipboard.writeText(v); m.textContent='✅ 已复制到剪贴板'; }
  catch(e){
    // 剪贴板不可用时退回选中文本，便于手动复制
    $('admKey').focus(); $('admKey').select();
    m.textContent='已选中密码，请长按手动复制';
  }
};
$('admKeyReset').onclick=async()=>{
  const m=$('admKeyMsg');
  if(!confirm('确定要重置管理员密码吗？将生成新的 16 位强随机密码，当前后台会话会失效，需要用新密码重新登录。')) return;
  m.textContent='重置中…';
  try{
    const d=await api('/api/admin/reset-key',{});
    $('admKey').value=d.key||'';
    m.textContent=(d.msg||'已重置')+'　新密码：'+d.key;
    alert('新管理员密码：\n'+d.key+'\n\n请立即保存！');
  }catch(e){ m.textContent='重置失败：'+e.message; }
};
$('go').onclick=async()=>{
  const u=$('user').value.trim(); const k=$('key').value.trim();
  if(!u||!k){ $('lmsg').textContent='请输入账号和密码'; return; }
  $('lmsg').textContent='验证中…'; $('lmsg').className='msg';
  try{ await api('/api/admin/login',{user:u,key:k}); $('loginBox').classList.add('hide'); $('panel').classList.remove('hide'); load(); }
  catch(e){ $('lmsg').textContent=e.message; }
};
$('key').addEventListener('keydown',e=>{ if(e.key==='Enter') $('go').click(); });
$('out').onclick=async()=>{ try{ await api('/api/admin/logout',{}); }catch(e){} goHome(); };
$('home').onclick=()=>{ goHome(); };
function goHome(){
  const sc=new URLSearchParams(location.search).get('sharecode')||'';
  location.href='/'+(sc?('?sharecode='+encodeURIComponent(sc)):'');
}

async function load(){
  let d;
  try{ d = await api('/api/admin/users'); }
  catch(e){ if(/未登录|登录/i.test(e.message)){ $('loginBox').classList.remove('hide'); $('panel').classList.add('hide'); } return; }
  const us=d.users||[];
  const pending=us.filter(x=>x.status==='pending').length;
  const ok=us.filter(x=>x.status==='approved').length;
  const vip=us.filter(x=>x.member).length;
  $('stat').innerHTML = '<div>注册总数 <b>'+us.length+'</b></div><div>待审核 <b style="color:#b26a00">'+pending+'</b></div><div>已通过 <b>'+ok+'</b></div><div>会员 <b style="color:#0f9d58">'+vip+'</b></div>';
  renderDl();
  if(!us.length){ $('list').textContent='暂无注册申请'; return; }
  $('list').innerHTML = us.map(u=>{
    const tg = u.status==='pending'?'<span class="tag t-p">待审核</span>':(u.status==='approved'?'<span class="tag t-a">已通过</span>':'<span class="tag t-r">已拒绝</span>');
    const gl = Array.isArray(u.member_grades)?u.member_grades:[];
    const isMem = u.member && gl.length>0;
    const vt = !isMem?'<span class="tag t-p">仅浏览</span>':(gl.length===3?'<span class="tag t-v">会员·全三册</span>':'<span class="tag t-v">会员·'+gl.join('/')+'</span>');
    const cur = !isMem?'未开通会员（仅浏览章节，不能做题、不能下载）':(gl.length===3?'已开通 全三册（高一/高二/高三）':'已开通「'+gl.join('、')+'」年级');
    const dt = u.dl?'<span class="tag t-a">下载已解锁</span>':'';
    return '<div class="row"><div class="u">'+esc(u.username)+tg+vt+dt+'</div>'+
      '<div class="m">注册：'+esc(u.created_at||'-')+'　到期：'+esc(u.expire||'-')+'<br>微信/手机：'+esc(u.phone||'-')+'　备注：'+esc(u.note||'-')+(u.invited_by?'<br>邀请人：'+esc(u.invited_by):'')+'<br><b style="color:#1565c0">当前：'+esc(cur)+'</b></div>'+
      '<div class="acts">'+
      (u.status!=='approved'?'<button class="btn s g" onclick="act(\''+u.username+'\',\'approve\')">通过</button>':'')+
      (u.status==='pending'?'<button class="btn s" style="background:#b26a00" onclick="act(\''+u.username+'\',\'reject\')">拒绝</button>':'')+
      '<span style="font-size:11.5px;color:#5c6878;margin-right:2px">单独开通年级：</span>'+
      ['高一','高二','高三'].map(g=>{
        const on = gl.indexOf(g)>=0;
        return '<button class="btn s '+(on?'':'g')+'" style="'+(on?'background:#0f9d58':'')+'" title="点击=只开通该年级（覆盖其它年级）" onclick="act(\''+u.username+'\',\'member_grade\',\''+g+'\')">'+g+(on?' ✓':'')+'</button>';
      }).join('')+
      '<button class="btn s" style="background:#1565c0;color:#fff" title="三个年级全部开通（学生付了 ¥16.9 全三册时点这个）" onclick="act(\''+u.username+'\',\'member_all\')">全开三册</button>'+
      (isMem?'<button class="btn s" style="background:#6b7787" onclick="act(\''+u.username+'\',\'unmember\')">取消会员</button>':'')+
      (!u.dl?'<button class="btn s g" title="仅开通下载权限（打赏/兑换码；会员已自动含下载）" onclick="dlAct(\'grant\',\''+u.username+'\')">仅开下载</button>':'<button class="btn s" style="background:#6b7787" onclick="dlAct(\'revoke\',\''+u.username+'\')">撤销下载</button>')+
      '<button class="btn s" style="background:#6b7787" onclick="act(\''+u.username+'\',\'reset\')">重置密码</button>'+
      '<button class="btn s r" onclick="act(\''+u.username+'\',\'del\')">删除</button>'+
      '</div></div>';
  }).join('');
}
async function act(u,a,g){
  if(a==='del' && !confirm('删除账号 '+u+' ？')) return;
  try{
    const d = await api('/api/admin/act',{u:u,act:a,g:g});
    if(d.password) alert('已重置密码：'+d.password+'\n请发给该同学，登录后建议自行修改。');
    load();
  }catch(e){ alert('操作失败：'+e.message); }
}

// ===== 下载权益（打赏申请 / 兑换码） =====
let DL=null;
async function renderDl(){
  const box=$('dlBox');
  try{ DL = await api('/api/admin/dl'); }
  catch(e){ box.textContent='加载失败：'+e.message; return; }
  const pend=(DL.requests||[]).filter(r=>r.status==='pending');
  const grants=DL.grants||[];
  const codes=DL.codes||[];
  const unused=codes.filter(c=>!c.used_by).length;
  let h='<div style="font-size:12.5px;color:#5c6878;margin-bottom:8px">已开通 <b style="color:#1565c0">'+grants.length+'</b> 人 ｜ 待处理申请 <b style="color:#b26a00">'+pend.length+'</b> ｜ 未用兑换码 <b style="color:#0f9d58">'+unused+'</b></div>';
  h+='<div class="tip" style="margin:0 0 10px;line-height:1.75">· <b>打赏 / 邀请 / 兑换码 = 只开通【资料下载】</b>权限，不含答题、背诵、赋分等题库功能；<br>· 学生若另付 <b>¥9.9（单年级）</b> 或 <b>¥16.9（全三册）</b> 开通会员，请点下方「开会员」快捷按钮，或到上方「用户列表」用年级按钮手动开通（<b>点「全开」= 三个年级全开通</b>）。</div>';
  if(pend.length){
    h+=pend.map(r=>'<div class="row"><div class="u">'+esc(r.username)+' <span class="tag t-p">申请码 '+esc(r.code)+'</span></div>'+
      '<div class="m">提交时间：'+esc(r.created_at||'-')+'</div><div class="acts">'+
      '<button class="btn s g" title="仅开通资料下载权限" onclick="dlAct(\'grant\',\''+esc(r.username)+'\')">开通下载</button>'+
      '<button class="btn s" style="background:#0f9d58" title="学生已付 ¥16.9 → 一次性开通高一+高二+高三全部题库功能" onclick="dlAct(\'member_all\',\''+esc(r.username)+'\')">开全三册会员</button>'+
      '<button class="btn s" style="background:#6b7787" onclick="dlAct(\'delreq\',\''+esc(r.username)+'\')">删除申请</button>'+
      '</div></div>').join('');
  }else{
    h+='<div style="margin-bottom:6px">暂无待处理申请（学生端提交打赏申请后会出现在这里）</div>';
  }
  if(grants.length){
    h+='<div style="font-size:12.5px;color:#5c6878;margin:10px 0 4px">已开通名单（点名字=撤销权益）</div>'+grants.map(g=>'<span class="tag t-v" style="margin:2px 4px 2px 0;cursor:pointer" title="点击撤销" onclick="dlAct(\'revoke\',\''+esc(g.username)+'\')">'+esc(g.username)+'（'+esc(g.source||'admin')+'）</span>').join('');
  }
  if(codes.length){
    h+='<div style="font-size:12.5px;color:#5c6878;margin:10px 0 4px">兑换码（绿色=未用，点击复制；划线=已用）</div><div style="line-height:2.2">'+
      codes.slice(0,50).map(c=>c.used_by
        ?'<span class="tag" style="background:#f0f2f5;color:#98a2b0;text-decoration:line-through">'+esc(c.code)+'</span>'
        :'<span class="tag t-a" style="margin:2px 4px 2px 0;cursor:pointer" title="点击复制" onclick="copyCode(\''+esc(c.code)+'\')">'+esc(c.code)+'</span>').join('')+'</div>';
  }
  box.innerHTML=h;
}
async function dlAct(act,u){
  if(act==='member_all' && !confirm('确认为 '+u+' 开通全三册会员？\n（高一+高二+高三 全部题库功能，一年内有效）')) return;
  try{ const d=await api('/api/admin/dl/act',{act:act,u:u}); alert(d.msg||'OK'); load(); }
  catch(e){ alert('操作失败：'+e.message); }
}
async function genCodes(){
  const n=prompt('生成几个兑换码？（1-100）','10');
  if(!n) return;
  try{
    const d=await api('/api/admin/dl/act',{act:'gencodes',n:parseInt(n,10)||10});
    if(d.codes&&d.codes.length) alert('已生成 '+d.codes.length+' 个兑换码（已保存在下方列表，点绿色码可复制）：\n\n'+d.codes.join('\n')+'\n\n请通过微信发给已打赏的同学。');
    load();
  }catch(e){ alert('生成失败：'+e.message); }
}
function copyCode(c){ try{ navigator.clipboard.writeText(c); }catch(e){} }
load();
</script></body></html>"""


# 各科资料来源说明（下载中心与打印版中展示，保证与网页内容可溯源）
DL_SRC = {
    "政治": "源文件：教材速通·必修1中特/必修2经济社会 ＋ 赋分术语（必修一14条、必修二25条）＋ 百年党史关键词 等 129 个课程资料，详见《01_政治精华总结》《04_资料使用指南》",
    "历史": "源文件：网络&智慧中小学生平台教材抠书课·纲要上 ＋ 阶段特征 ＋ 中国史重大事件时间线 ＋ 历史概念一点通 等 193 个课程资料，详见《02_历史精华总结》《04_资料使用指南》",
    "地理": "源文件：考前必记自然地理32个核心考点 ＋ 元素串讲【地气水土生】＋ 人文地理大口诀 等 24 个课程资料，详见《03_地理精华总结》《04_资料使用指南》",
    "生物": "源文件：必修1《分子与细胞》教材同步整理，与网站「资料」页的学习资料/思维导图同源",
}

# 学科思维导图连线美化：贝塞尔曲线 → 直角圆角折线（XMind 风格，打印更清晰）
def mm_elbow(svg):
    if not svg or "<path" not in svg:
        return svg or ""

    def one(m):
        try:
            n = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]
            if len(n) < 8:
                return m.group(0)
            sx, sy, ex, ey = n[0], n[1], n[-2], n[-1]
        except Exception:
            return m.group(0)
        midx = (sx + ex) / 2.0
        dy = ey - sy
        if abs(dy) < 1.5:
            return 'd="M%.1f,%.1f H%.1f"' % (sx, sy, ex)
        r = min(9.0, abs(dy) / 2.0, abs(midx - sx) / 2.0, abs(ex - midx) / 2.0)
        s1 = 1 if dy > 0 else -1
        d = ("M%.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f"
             % (sx, sy, midx - r, midx, sy, midx, sy + s1 * r,
                ey - s1 * r, midx, ey, midx + r, ey, ex))
        return 'd="%s"' % d

    return re.sub(r'd="(M-?[\d.,]+[^"]*)"', one, svg)


# ============================================================ HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "ZBStudy/2.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def _cors(self):
        # 仅对可信源回显 CORS（同源部署不触发 Origin，故不影响本站）；杜绝任意站点借登录态读接口
        origin = self.headers.get("Origin")
        if origin and _cors_origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Content-Ticket")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def j(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 20000:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            pass
        return c

    def cur_user(self):
        m = self.cookies().get("zb_sid")
        if not m:
            return None
        u = read_sid(m.value)
        if not u:
            return None
        try:
            return get_user(u)
        except Exception:
            return None

    def client_ip(self):
        xff = self.headers.get("X-Forwarded-For") or ""
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def is_admin(self):
        m = self.cookies().get("zb_adm")
        return bool(m) and hmac.compare_digest(m.value, admin_sig())

    def _cookie_attrs(self, max_age, admin=False):
        # 关键修复：浏览器在 HTTP 页面会拒绝存储带 Secure 的 Cookie，
        # 导致后台/登录“点了没反应”（Cookie 写不进去，后续请求拿不到会话）。
        # 因此：HTTPS 下用 SameSite=None;Secure（跨站安全）；HTTP 下改用 SameSite=Lax（无需 Secure，同源可用）。
        # 后台 Cookie 一律 SameSite=Lax：后台从不被跨站访问，收紧到同源，降低被第三方站点携带的风险。
        if admin:
            return "Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % max_age
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        https = proto == "https"
        same_site = "None" if https else "Lax"
        secure = "; Secure" if https else ""
        return "Path=/; Max-Age=%d; HttpOnly; SameSite=%s%s" % (max_age, same_site, secure)

    def _cookie_multi(self, pairs):
        """拼接多个 Set-Cookie（pairs = [(名, 值, max_age), ...]）。

        注意：_cookie_attrs() 返回的字符串末尾没有分号，直接拼接会让两个 Cookie 粘成
        "SameSite=Laxzb_rt=xxx"，导致浏览器只认到第一个 Cookie（长期令牌写不进去）。
        这里统一用 "; " 分隔，保证每个 Cookie 都是独立的一条。
        """
        out = []
        for name, val, max_age in pairs:
            out.append("%s=%s; %s" % (name, val, self._cookie_attrs(max_age)))
        return "; ".join(out)

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                return self.serve_index()
            if path in ("/admin", "/admin/"):
                return self._send(200, ADMIN_HTML.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store"})
            if path == "/api/health":
                return self.j(200, {"ok": True, "mode": "server", "price": PRICE,
                                    "wx": WX_NAME, "area": WX_AREA})
            if path == "/api/auto-login":
                return self.api_auto_login()
            if path == "/api/me":
                u = self.cur_user()
                if not u:
                    return self.j(200, {"ok": False, "login": False})
                act = member_active(u)
                mi = member_info(u)
                return self.j(200, {"ok": True, "login": True, "u": u["username"],
                                    "status": u.get("status"), "member": act,
                                    "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"],
                                    "expire": u.get("expire") or "", "dl": has_dl(u["username"]) or act})
            if path == "/api/content":
                return self.api_content()
            if path == "/api/content/ticket":
                return self.api_content_ticket()
            if path == "/api/bootstrap":
                return self.api_bootstrap()
            if path == "/api/rec":
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                return self.api_rec_get((qs.get("kind") or [""])[0])
            if path == "/api/rec/all":
                return self.api_rec_get(None)
            if path == "/api/checkin":
                return self.api_checkin_get()
            if path == "/api/rank":
                return self.api_rank()
            if path == "/api/friends":
                return self.api_friends()
            if path == "/api/msg":
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                _since = (qs.get("since") or [None])[0]
                try:
                    _since = int(_since) if _since not in (None, "") else None
                except Exception:
                    _since = None
                return self.api_msg_get((qs.get("with") or [""])[0], _since)
            if path == "/api/challenge":
                return self.api_challenge_get()
            if path == "/api/dash":
                return self.api_dash()
            if path == "/api/ai/state":
                return self.api_ai_state()
            if path == "/api/dl/status":
                return self.api_dl_status()
            if path == "/api/invite/stat":
                return self.api_invite_stat()
            if path == "/api/dl/files":
                return self.api_dl_files()
            if path == "/api/admin/dl":
                return self.api_admin_dl()
            if path == "/download/file":
                return self.serve_download_file()
            if path == "/download/print":
                return self.api_dl_print()
            if path == "/download/zip":
                return self.serve_download_zip()
            if path == "/api/admin/users":
                return self.api_admin_users()
            if path == "/api/admin/key":
                return self.api_admin_key()
            if path == "/api/admin/tables":
                return self.api_admin_tables()
            if path == "/api/admin/rows":
                return self.api_admin_rows()
            if path == "/admin/tables":
                return self.serve_admin_tables()
            if path == "/favicon.ico":
                # 有 favicon-32.png 时直接返回，否则沿用旧的空响应
                _fp = os.path.join(SITE_DIR, "favicon-32.png")
                if os.path.isfile(_fp):
                    return self.serve_file(_fp, "image/png", cache="public, max-age=86400")
                return self._send(204, b"")
            # 静态资源（PWA：manifest / 图标 / img 目录）。仅白名单扩展名、限 SITE_DIR 内、禁止目录穿越
            if path.startswith("/"):
                ext = os.path.splitext(path)[1].lower()
                ctype_map = {
                    ".webmanifest": "application/manifest",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                    ".svg": "image/svg+xml",
                    ".css": "text/css",
                    ".js": "application/javascript",
                    ".txt": "text/plain",
                }
                # 安全：白名单刻意不含 ".json" / ".gz"。
                # site/ 下唯一的 json 是 content.json（付费题库源文件，2.3MB），
                # 必须经 /api/content 走「登录 + 审核通过 + 已开会员 + 一次性票据」校验后下发；
                # 若静态分支放行 .json，未登录一次请求即可拿走全部 2789 道题 / 931 卡片，
                # 等于整套付费内容零成本泄漏。改为按需加白名单，而不是靠扩展名黑名单。
                # 允许一级子目录（如 /img/xxx.png），但仍限制在 SITE_DIR 内
                rel = path.lstrip("/")
                if ext in ctype_map and rel.count("/") <= 1 and ".." not in rel:
                    fpath = os.path.normpath(os.path.join(SITE_DIR, rel))
                    if fpath.startswith(SITE_DIR + os.sep) and os.path.isfile(fpath):
                        return self.serve_file(fpath, ctype_map[ext], cache="public, max-age=86400")
            return self.j(404, {"msg": "not found"})
        except TursoError as e:
            self.j(503, {"msg": "数据库暂时不可用，请稍后再试", "detail": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:
            self.j(500, {"msg": "服务器错误", "detail": str(e)})

    def do_POST(self):
        path = self.path.split("?")[0]
        ip = self.client_address[0]
        try:
            if path == "/api/register":
                return self.api_register(ip)
            if path == "/api/change-password":
                return self.api_change_password()
            if path == "/api/admin/reset-key":
                return self.api_admin_reset_key()
            if path == "/api/login":
                return self.api_login(ip)
            if path == "/api/logout":
                # 登出：撤销全部长期令牌（"记住我" 失效），并清除两个 Cookie
                try:
                    m = self.cookies().get("zb_sid")
                    if m:
                        u = read_sid(m.value)
                        if u:
                            revoke_refresh_tokens(u)
                except Exception:
                    pass
                return self.j(200, {"ok": True},
                              {"Set-Cookie": self._cookie_multi([
                                  ("zb_sid", "", 0), (REFRESH_COOKIE, "", 0)])})
            if path == "/api/admin/login":
                return self.api_admin_login()
            if path == "/api/admin/logout":
                return self.j(200, {"ok": True}, {"Set-Cookie": "zb_adm=; " + self._cookie_attrs(0)})
            if path == "/api/admin/act":
                return self.api_admin_act()
            if path == "/api/admin/sql":
                return self.api_admin_sql()
            if path == "/api/rec":
                return self.api_rec_set()
            if path == "/api/rec/bulk":
                return self.api_rec_bulk()
            if path == "/api/recite":
                return self.api_recite()
            if path == "/api/checkin":
                return self.api_checkin_post()
            if path == "/api/friend/request":
                return self.api_friend_request()
            if path == "/api/friend/accept":
                return self.api_friend_accept()
            if path == "/api/friend/reject":
                return self.api_friend_reject()
            if path == "/api/msg/send":
                return self.api_msg_send()
            if path == "/api/challenge/new":
                return self.api_challenge_new()
            if path == "/api/challenge/result":
                return self.api_challenge_result()
            if path == "/api/dl/request":
                return self.api_dl_request()
            if path == "/api/dl/redeem":
                return self.api_dl_redeem()
            if path == "/api/invite/stat":
                return self.api_invite_stat()
            if path == "/api/invite/claim":  # 兼容旧前端：阶梯奖励已改为自动发放
                return self.api_invite_claim()
            if path == "/api/admin/dl/act":
                return self.api_admin_dl_act()
            if path == "/api/ai/bind":
                return self.api_ai_bind()
            if path == "/api/ai/chat":
                return self.api_ai_chat()
            return self.j(404, {"msg": "not found"})
        except TursoError as e:
            self.j(503, {"msg": "数据库暂时不可用，请稍后再试", "detail": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:
            self.j(500, {"msg": "服务器错误", "detail": str(e)})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 静态文件 ----------
    def serve_file(self, path, ctype, cache="no-store"):
        if not os.path.exists(path):
            return self.j(404, {"msg": "缺少 %s，请先运行 build_site.py 生成" % os.path.basename(path)})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype, {"Cache-Control": cache})

    def serve_index(self):
        """首页：gzip 压缩 + ETag 协商缓存。
        原实现每次都 no-store 全量重传（首页约 200KB）。现改为：
        - 传输层 gzip（浏览器自动解压，传输量降约 70%）
        - 强 ETag + If-None-Match 协商：内容未变时回 304，0 字节传输
        - Cache-Control 允许短时缓存，避免每次刷新都打服务端
        """
        if not os.path.exists(INDEX_PATH):
            return self.j(404, {"msg": "缺少 index.html"})
        try:
            mtime = os.path.getmtime(INDEX_PATH)
        except OSError:
            mtime = 0
        etag = '"%x-%x"' % (int(mtime), INDEX_PATH.__hash__() & 0xffff)
        # 协商命中：直接 304，不传 body
        if self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
            self._cors()
            self.end_headers()
            return
        with open(INDEX_PATH, "rb") as f:
            raw = f.read()
        # 协商 gzip：客户端支持才压
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        extra = {"ETag": etag, "Cache-Control": "public, max-age=0, must-revalidate"}
        if "gzip" in accept:
            blob = gzip.compress(raw, 6)
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
            return self._send(200, blob, "text/html; charset=utf-8", extra)
        return self._send(200, raw, "text/html; charset=utf-8", extra)

    # ---------- 云端记录（进度/练习/错题，按字段存 Turso） ----------
    def _rec_user(self):
        u = self.cur_user()
        if not u:
            self.j(401, {"code": "LOGIN", "msg": "请先登录"})
            return None
        if (u.get("status") or "") != "approved":
            self.j(403, {"code": "PENDING", "msg": "账号待审核"})
            return None
        return u["username"]

    def api_bootstrap(self):
        """启动聚合接口：一次 DB 往返返回启动所需的全部数据。

        性能背景：Turso 单次往返 0.9~1.8 秒。原启动流程并发打 5~6 次库
        （/api/me、/api/rec/all、/api/dl/status、/api/dash…），累计 3~6 秒，
        是用户感知"卡"的主因。现用 Turso pipeline 把 4 条查询合并成 1 次往返
        （实测 3 条批量 876ms vs 3×1200ms）。
        返回结构兼容原有各接口，前端可逐步切换。
        """
        m = self.cookies().get("zb_sid")
        if not m:
            return self.j(200, {"ok": False, "login": False})
        uname = read_sid(m.value)
        if not uname:
            return self.j(200, {"ok": False, "login": False})
        # 一次批量取回：用户 / 全部记录 / 下载权益 / 下载申请 / 打卡记录
        try:
            urows, recs, dls, dlreqs, chks = turso_multi([
                ("SELECT * FROM zb_users WHERE username = ?", [(uname, "text")]),
                ("SELECT kind,key,val,updated FROM zb_records WHERE username=?", [(uname, "text")]),
                ("SELECT username FROM zb_dl WHERE username=?", [(uname, "text")]),
                ("SELECT code,created_at,status FROM zb_dl_req WHERE username=?", [(uname, "text")]),
                ("SELECT day FROM zb_checkin WHERE username=?", [(uname, "text")]),
            ])
        except TursoError:
            return self.j(503, {"msg": "数据库暂时不可用，请稍后再试"})
        u = urows[0] if urows else None
        if not u:
            return self.j(200, {"ok": False, "login": False})
        # 回填缓存，省掉后续 /api/me 等接口的查询
        try:
            with _USER_CACHE["lock"]:
                _USER_CACHE["d"][uname] = (time.time(), u)
        except Exception:
            pass
        act = member_active(u)
        mi = member_info(u)
        granted = bool(dls) or act
        req = None
        if dlreqs and (dlreqs[0].get("status") or "pending") == "pending":
            req = {"code": dlreqs[0].get("code"), "created_at": dlreqs[0].get("created_at") or ""}
        items = [{"kind": r["kind"], "key": r["key"], "val": r["val"], "updated": r.get("updated")}
                 for r in recs]
        days = [c["day"] for c in chks]
        # 仪表盘数据也在这里一并算好（复用同一批记录行，不额外查库）
        dash = self._dash_from_rows(recs, days, act, mi)
        self.j(200, {
            "ok": True, "login": True, "u": uname,
            "status": u.get("status"), "member": act,
            "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"],
            "expire": u.get("expire") or "", "dl": granted,
            "items": items,
            "dl_req": req,
            "dash": dash,
        })

    def api_rec_get(self, kind):
        uname = self._rec_user()
        if not uname:
            return
        if kind:
            rows = turso("SELECT key,val,updated FROM zb_records WHERE username=? AND kind=?",
                        [(uname, "text"), (kind, "text")])
            items = [{"key": r["key"], "val": r["val"], "updated": r.get("updated")} for r in rows]
        else:
            rows = turso("SELECT kind,key,val,updated FROM zb_records WHERE username=?", [(uname, "text")])
            items = [{"kind": r["kind"], "key": r["key"], "val": r["val"], "updated": r.get("updated")} for r in rows]
        self.j(200, {"ok": True, "items": items})

    def api_rec_set(self):
        uname = self._rec_user()
        if not uname:
            return
        d = self.body_json()
        kind = (d.get("kind") or "").strip()
        key = (d.get("key") or "").strip()
        val = d.get("val")
        if not kind or not key:
            return self.j(400, {"msg": "kind 和 key 必填"})
        val_s = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("INSERT INTO zb_records (username,kind,key,val,updated) VALUES (?,?,?,?,?) "
              "ON CONFLICT(username,kind,key) DO UPDATE SET val=excluded.val, updated=excluded.updated",
              [(uname, "text"), (kind, "text"), (key, "text"), (val_s, "text"), (ts, "text")])
        # 标记"有使用痕迹"（被邀请人的学习行为）—— 僵尸号判定用
        try:
            turso("UPDATE zb_invites SET acted=1 WHERE invitee=? AND acted=0", [(uname, "text")])
        except Exception:
            pass
        self.j(200, {"ok": True})

    def api_rec_bulk(self):
        uname = self._rec_user()
        if not uname:
            return
        d = self.body_json()
        items = d.get("items") or []
        if not isinstance(items, list):
            return self.j(400, {"msg": "items 必须是数组"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        args = []
        for it in items[:500]:
            kind = str(it.get("kind") or "").strip()
            key = str(it.get("key") or "").strip()
            if not kind or not key:
                continue
            val = it.get("val")
            val_s = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
            args += [(uname, "text"), (kind, "text"), (key, "text"), (val_s, "text"), (ts, "text")]
        if args:
            placeholders = ",".join(["(?,?,?,?,?)"] * (len(args) // 5))
            turso("INSERT INTO zb_records (username,kind,key,val,updated) VALUES " + placeholders +
                  " ON CONFLICT(username,kind,key) DO UPDATE SET val=excluded.val, updated=excluded.updated",
                  args)
        # 标记"有使用痕迹"（被邀请人的学习行为）—— 僵尸号判定用
        try:
            turso("UPDATE zb_invites SET acted=1 WHERE invitee=? AND acted=0", [(uname, "text")])
        except Exception:
            pass
        self.j(200, {"ok": True, "saved": len(args) // 5})

    # ---------- 学习仪表盘（免费=当前册数据；会员=全册诊断，前端按 member 区分展示） ----------
    def _dash_from_rows(self, rows, days, premium, mi):
        """由 zb_records 行 + 打卡天数聚合出仪表盘数据（纯计算，无 DB 调用）。"""
        m = qmap()
        per = {}          # subject -> {att,right,vols:{vol:{att,right}}}
        wrong_recent = []  # 近 20 条错题（按记录更新时间倒序）
        wrong_by_subj = {}  # 学科 -> 错题总数
        wrong_tags = {}     # 错因标签 -> 计数
        known_by_subj = {}
        for r in rows:
            k, key, val = r["kind"], r["key"] or "", r["val"] or ""
            if k == "done":
                meta = m.get(key)
                if not meta:
                    continue
                try:
                    d = json.loads(val) if isinstance(val, str) and val.startswith("{") else (val if isinstance(val, dict) else {})
                except Exception:
                    d = {}
                n = int(d.get("n") or 0)
                rr = int(d.get("r") or 0)
                if n <= 0:
                    continue
                sub = meta["subject"] or "其他"
                bucket = per.setdefault(sub, {"att": 0, "right": 0, "vols": {}})
                bucket["att"] += n
                bucket["right"] += rr
                vb = bucket["vols"].setdefault(meta["volume"] or "未分册", {"att": 0, "right": 0})
                vb["att"] += n
                vb["right"] += rr
            elif k == "wrong":
                meta = m.get(key)
                ws = (meta or {}).get("subject") or "其他"
                wrong_by_subj[ws] = wrong_by_subj.get(ws, 0) + 1
                wrong_recent.append({
                    "id": key,
                    "subject": (meta or {}).get("subject") or "",
                    "volume": (meta or {}).get("volume") or "",
                    "unit": (meta or {}).get("unit") or "",
                    "updated": r.get("updated") or "",
                })
            elif k == "known":
                sub = key.split("@@")[0] if "@@" in key else "其他"
                known_by_subj[sub] = known_by_subj.get(sub, 0) + 1
            elif k == "wrong_tag":
                try:
                    tv = json.loads(val) if isinstance(val, str) and val.startswith("{") else (val if isinstance(val, dict) else {})
                except Exception:
                    tv = {}
                tag = tv.get("tag") or "未标注"
                wrong_tags[tag] = wrong_tags.get(tag, 0) + 1
        wrong_recent.sort(key=lambda x: x["updated"], reverse=True)
        wrong_recent = wrong_recent[:50]
        for sub in per.values():
            sub["rate"] = round(sub["right"] * 100 / sub["att"]) if sub["att"] else 0
            for vb in sub["vols"].values():
                vb["rate"] = round(vb["right"] * 100 / vb["att"]) if vb["att"] else 0
        return {
            "ok": True, "member": premium,
            "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"],
            "per": per,
            "known_by_subj": known_by_subj,
            "known_total": sum(known_by_subj.values()),
            "wrong_total": len([r for r in rows if r["kind"] == "wrong"]),
            "wrong_by_subj": wrong_by_subj,
            "wrong_tags": wrong_tags,
            "wrong_recent": wrong_recent[:20],
            "checkin_days": len(set(days)),
            "streak": self._streak(list(set(days))),
        }

    def api_dash(self):
        uname = self._rec_user()
        if not uname:
            return
        u = get_user(uname)
        premium = member_active(u) if u else False
        mi = member_info(u) if u else {"tier": "", "grade": "", "grades": []}
        rows, crows = turso_multi([
            ("SELECT kind,key,val,updated FROM zb_records WHERE username=?", [(uname, "text")]),
            ("SELECT day FROM zb_checkin WHERE username=?", [(uname, "text")]),
        ])
        days = [c["day"] for c in crows]
        self.j(200, self._dash_from_rows(rows, days, premium, mi))

    # ---------- AI 助手（用户自绑主流厂商 Key，服务端代理，密钥不下发） ----------
    AI_PRESETS = {
        # 统一走 OpenAI 兼容 chat/completions，改 base/model 即可适配主流厂商
        # models：各厂商「当前可调用」的模型名清单（2026-10 官方文档快照，随厂商更新）；前端以下拉方式呈现，用户不必手敲
        "doubao":  {"name": "豆包·火山方舟", "base": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-2-0-mini-260428",
                    "hint": "豆包需在火山方舟创建「推理接入点」，实际对话请填 ep-xxxx-xxxx 接入点 ID；下列官方模型名也可直接选",
                    "doc": "https://www.volcengine.com/docs/82379/1553576",
                    "models": ["doubao-seed-2-0-mini-260428","doubao-seed-2-0-lite-260428","doubao-seed-2-0-pro-260215",
                               "doubao-seed-2-1-lite-260915","doubao-seed-2-1-pro-260915","doubao-seed-2-1-turbo-260628",
                               "doubao-seed-2-0-code-preview-260215","doubao-seed-evolving"]},
        "deepseek":{"name": "DeepSeek",      "base": "https://api.deepseek.com", "model": "deepseek-v4-flash",
                    "hint": "deepseek-v4-flash（通用/低延迟）/ deepseek-v4-pro（深度思考）；旧名 deepseek-chat、deepseek-reasoner 已于 2026-07-24 弃用。到官网控制台复制 API Key 即可用",
                    "doc": "https://api-docs.deepseek.com/",
                    "models": ["deepseek-v4-flash","deepseek-v4-pro"]},
        "glm":     {"name": "智谱 GLM",      "base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.7-flash",
                    "hint": "glm-4.7-flash / glm-5.3-flash 有免费额度，注册即送；glm-5.3 为最新旗舰",
                    "doc": "https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8%E5%BC%82%E6%AD%A5",
                    "models": ["glm-4.7-flash","glm-5.3-flash","glm-5.3","glm-5.2","glm-5.1","glm-5","glm-5-turbo",
                               "glm-4.7","glm-4.6","glm-4.5-air","glm-4.5-airx","glm-4-flash-250414"]},
        "kimi":    {"name": "Kimi·月之暗面", "base": "https://api.moonshot.cn/v1", "model": "kimi-k3",
                    "hint": "kimi-k3 为当前旗舰（1M 上下文）；kimi-k2.7-code 适合编程任务。moonshot-v1 / kimi-k2 系列已下线",
                    "doc": "https://platform.moonshot.cn/docs/pricing/chat-k25",
                    "models": ["kimi-k3","kimi-k3.1-pro","kimi-k3.1-pro-highspeed","kimi-k2.7-code",
                               "kimi-k2.7-code-highspeed","kimi-k2.6"]},
        "qwen":    {"name": "通义千问",      "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.8-flash",
                    "hint": "qwen3.8-flash / qwen3.7-plus / qwen3.8-max；阿里云百炼控制台取 API Key",
                    "doc": "https://help.aliyun.com/zh/model-studio/models",
                    "models": ["qwen3.8-flash","qwen3.7-flash","qwen3.6-flash","qwen3.7-plus","qwen3.6-plus",
                               "qwen3.8-max","qwen3.7-max","qwen3.7-turbo","qwen3-max","qwen-plus","qwen-flash",
                               "qwen-turbo","qwen-long","qwen3-coder-plus","qwen3-coder-flash","qwq-plus"]},
        "custom":  {"name": "自定义 OpenAI 兼容接口", "base": "", "model": "",
                    "hint": "填你自己的接口地址（以 /v1 结尾）与模型名，可接任意 OpenAI 兼容服务",
                    "doc": "",
                    "models": []},
    }
    AI_FREE_QUOTA = 20       # 免费用户每天问答次数
    AI_MEMBER_QUOTA = 1000   # 会员每天问答次数（实际上限足够，视为不限）
    _AI_KEY_MASK = "***"

    def _ai_bind(self, uname):
        rows = turso("SELECT val FROM zb_records WHERE username=? AND kind='ai_bind' AND key='cfg'",
                     [(uname, "text")])
        if not rows:
            return None
        try:
            return json.loads(rows[0]["val"])
        except Exception:
            return None

    def api_ai_state(self):
        uname = self._rec_user()
        if not uname:
            return
        b = self._ai_bind(uname)
        u = get_user(uname)
        premium = member_active(u) if u else False
        mi = member_info(u) if u else {"tier": "", "grade": "", "grades": []}
        unlimited = bool(b)  # 自绑 Key = 用自己的额度，不限量（会员或非会员都一样）
        quota = self.AI_MEMBER_QUOTA if (premium or unlimited) else self.AI_FREE_QUOTA
        today = self._today()
        used = 0
        rows = turso("SELECT val FROM zb_records WHERE username=? AND kind='ai_quota' AND key=?",
                     [(uname, "text"), (today, "text")])
        if rows:
            try:
                used = int(json.loads(rows[0]["val"]).get("n") or 0)
            except Exception:
                used = 0
        key_mask = ""
        if b:
            k = b.get("key") or ""
            key_mask = (k[:4] + "****" + k[-4:]) if len(k) > 8 else "****"
        self.j(200, {"ok": True, "bound": bool(b),
                     "provider": (b or {}).get("provider") or "",
                     "model": (b or {}).get("model") or "",
                     "key_mask": key_mask,
                     "used": used, "quota": quota, "member": premium, "unlimited": unlimited,
                     "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"],
                     "member_tier": mi["tier"], "member_grade": mi["grade"],
                     "presets": {k: {"name": v["name"], "base": v["base"], "model": v["model"], "hint": v["hint"],
                                     "doc": v.get("doc", ""), "models": v.get("models", [])}
                                 for k, v in self.AI_PRESETS.items()}})

    def api_ai_bind(self):
        uname = self._rec_user()
        if not uname:
            return
        d = self.body_json()
        provider = (d.get("provider") or "").strip()
        key = (d.get("key") or "").strip()
        model = (d.get("model") or "").strip()
        base = (d.get("base") or "").strip()
        if provider not in self.AI_PRESETS:
            return self.j(400, {"msg": "不支持的服务商"})
        if not key:
            return self.j(400, {"msg": "请填写 API Key"})
        p = self.AI_PRESETS[provider]
        if not base:
            base = p["base"]
        if not model:
            model = p["model"]
        # 自定义接口必须用户自己填 base 与 model
        if provider == "custom" and (not base or not model):
            return self.j(400, {"msg": "自定义接口需填写「接口地址」与「模型名」"})
        if not base.startswith("https://"):
            return self.j(400, {"msg": "接口地址必须为 https:// 开头"})
        if len(key) > 300 or len(model) > 80 or len(base) > 200:
            return self.j(400, {"msg": "配置项过长"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("INSERT INTO zb_records (username,kind,key,val,updated) VALUES (?,?,?,?,?) "
              "ON CONFLICT(username,kind,key) DO UPDATE SET val=excluded.val, updated=excluded.updated",
              [(uname, "text"), ("ai_bind", "text"), ("cfg", "text"),
               (json.dumps({"provider": provider, "base": base, "model": model, "key": key},
                           ensure_ascii=False), "text"), (ts, "text")])
        self.j(200, {"ok": True, "msg": "AI 助手绑定成功"})

    def api_ai_chat(self):
        uname = self._rec_user()
        if not uname:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        b = self._ai_bind(uname)
        if not b:
            return self.j(403, {"code": "NOBIND", "msg": "请先在 AI 助手设置中绑定你的大模型 API Key"})
        u = get_user(uname)
        premium = member_active(u) if u else False
        unlimited = bool(b)  # 自绑 Key = 用自己的额度，不限量
        quota = self.AI_MEMBER_QUOTA if (premium or unlimited) else self.AI_FREE_QUOTA
        today = self._today()
        used = 0
        rows = turso("SELECT val FROM zb_records WHERE username=? AND kind='ai_quota' AND key=?",
                     [(uname, "text"), (today, "text")])
        if rows:
            try:
                used = int(json.loads(rows[0]["val"]).get("n") or 0)
            except Exception:
                used = 0
        if not unlimited and used >= quota:
            return self.j(429, {"code": "QUOTA", "msg": "今日 %d 次问答已用完，明天再来，或开通会员提升额度" % quota})
        d = self.body_json()
        msgs_in = d.get("msgs") or []
        if not isinstance(msgs_in, list) or not msgs_in:
            return self.j(400, {"msg": "缺少对话内容"})
        # 只保留最近 12 轮，内容截断，防滥用
        clean = []
        for it in msgs_in[-12:]:
            role = it.get("role") or "user"
            content = str(it.get("content") or "")[:2000]
            if role in ("user", "assistant") and content:
                clean.append({"role": role, "content": content})
        if not clean:
            return self.j(400, {"msg": "缺少对话内容"})
        ctx = d.get("ctx") or {}
        subject = str(ctx.get("subject") or "")[:10]
        unit = str(ctx.get("unit") or "")[:30]
        grade = str(ctx.get("grade") or "")[:6]
        sys_prompt = (
            "你是一名经验丰富的广东高中政治·历史·地理·生物辅导老师（新高考3+1+2）。"
            "当前学生信息：%s%s%s。回答要求："
            "①用简体中文，先给结论再展开；②分点作答，善用赋分术语（原理+方法论+材料、背景+内容+影响、自然+人文+区位）；"
            "③涉及背诵时给出可背的要点句；④不定政治立场错误言论，不讨论敏感话题；"
            "⑤回答控制在 400 字内，除非学生要求详细讲解。" % (
                ("年级：%s。" % grade) if grade else "",
                ("科目：%s。" % subject) if subject else "",
                ("正在学习单元：%s。" % unit) if unit else "",
            )
        )
        payload = {
            "model": b.get("model") or "",
            "messages": [{"role": "system", "content": sys_prompt}] + clean,
            "temperature": 0.6,
            "max_tokens": 1200,
        }
        url = (b.get("base") or "").rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + (b.get("key") or "")},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                msg = (err.get("error") or {}).get("message") or err.get("message") or ("服务商返回 %d" % e.code)
            except Exception:
                msg = "服务商返回 %d，请检查 Key 与模型名" % e.code
            low = (msg or "").lower()
            if "does not exist" in low or "exist" in low or "无访问" in msg or "access" in low:
                msg += "｜模型/接入点不存在：豆包请在火山方舟创建「推理接入点」并把模型名填成 ep-xxxx；"
                msg += "其他厂商请核对模型名是否拼写正确、是否对该账号开放"
            elif "401" in msg or "unauthorized" in low or "api key" in low or "invalid" in low:
                msg += "｜API Key 无效或权限不足，请到对应平台核对 Key 与余额"
            elif "404" in msg or "not found" in low:
                msg += "｜接口地址或路径不正确，自定义接口请以 /v1 或 /v3 等结尾、且含 /chat/completions"
            return self.j(502, {"code": "UPSTREAM", "msg": "模型调用失败：" + msg})
        except Exception as e:
            return self.j(502, {"code": "UPSTREAM", "msg": "模型调用失败：" + str(e)[:120]})
        reply = ""
        try:
            reply = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            return self.j(502, {"code": "UPSTREAM", "msg": "模型返回格式异常"})
        if not reply:
            return self.j(502, {"code": "UPSTREAM", "msg": "模型返回为空"})
        # 计数 +1（按天）
        nt = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("INSERT INTO zb_records (username,kind,key,val,updated) VALUES (?,?,?,?,?) "
              "ON CONFLICT(username,kind,key) DO UPDATE SET val=excluded.val, updated=excluded.updated",
              [(uname, "text"), ("ai_quota", "text"), (today, "text"),
               (json.dumps({"n": used + 1}), "text"), (nt, "text")])
        self.j(200, {"ok": True, "reply": reply, "used": used + 1, "quota": quota})

    # ---------- 签到 + 学习排行榜（互动激励） ----------
    # 积分规则（透明，前端同步展示）：
    #   签到 +10 分/天；连续签到第 3 天起每天额外 +2×连续天数（封顶 +20）
    #   练习每题次 +2 分，答对再 +1 分；背诵过关每张 +5 分
    CHECKIN_BASE = 10
    CHECKIN_STREAK_STEP = 2
    CHECKIN_STREAK_MAX = 20
    PTS_PER_QUIZ = 2
    PTS_PER_RIGHT = 1
    PTS_PER_CARD = 5

    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _streak(self, days, today=None):
        """days=该用户全部签到日列表；返回当前连续签到天数（今天未签则看是否昨天签过）"""
        today = today or self._today()
        s = set(days)
        try:
            cur = datetime.date.fromisoformat(today)
        except Exception:
            return 0
        if today not in s:
            cur = cur - datetime.timedelta(days=1)
            if cur.isoformat() not in s:
                return 0
        n = 0
        while cur.isoformat() in s:
            n += 1
            cur -= datetime.timedelta(days=1)
        return n

    def api_checkin_get(self):
        uname = self._rec_user()
        if not uname:
            return
        rows = turso("SELECT day FROM zb_checkin WHERE username=?", [(uname, "text")])
        days = [r["day"] for r in rows]
        self.j(200, {"ok": True, "today": self._today(), "checked": self._today() in days,
                     "streak": self._streak(days), "days": len(days)})

    def api_checkin_post(self):
        uname = self._rec_user()
        if not uname:
            return
        today = self._today()
        rows = turso("SELECT day FROM zb_checkin WHERE username=?", [(uname, "text")])
        days = [r["day"] for r in rows]
        if today in days:
            st = self._streak(days, today)
            return self.j(200, {"ok": True, "already": True, "day": today, "streak": st,
                                "gained": 0, "msg": "今天已签到，已连续 %d 天" % st})
        new_streak = self._streak(days, today) + 1
        gained = self.CHECKIN_BASE
        if new_streak >= 3:
            gained += min(new_streak * self.CHECKIN_STREAK_STEP, self.CHECKIN_STREAK_MAX)
        turso("INSERT INTO zb_checkin (username,day,gained,ts) VALUES (?,?,?,?) "
              "ON CONFLICT(username,day) DO UPDATE SET gained=excluded.gained, ts=excluded.ts",
              [(uname, "text"), (today, "text"), (gained, "int"),
               (time.strftime("%Y-%m-%d %H:%M:%S"), "text")])
        msg = "签到成功 +%d 分" % gained
        if new_streak >= 3:
            msg += "（连签 %d 天，奖励 +%d）" % (new_streak, gained - self.CHECKIN_BASE)
        self.j(200, {"ok": True, "already": False, "day": today, "streak": new_streak,
                     "gained": gained, "msg": msg})

    def api_rank(self):
        uname = self._rec_user()
        if not uname:
            return
        # 性能：原来 3 次串行全表查询（每次约 1~1.8 秒）→ 合并成 1 次 pipeline 往返
        users, recs, chks, chals = turso_multi([
            ("SELECT username FROM zb_users WHERE status='approved'", None),
            ("SELECT username,kind,val FROM zb_records", None),
            ("SELECT username,day,gained FROM zb_checkin", None),
            ("SELECT frm,to_u,frm_score,to_score,status FROM zb_challenges", None),
        ])
        # 挑战胜负：每用户 [胜, 负]
        wl = {}
        for c in chals:
            if c.get("status") != "done":
                continue
            fs = c.get("frm_score"); ts = c.get("to_score")
            if fs is None or ts is None:
                continue
            frm = c.get("frm"); to = c.get("to_u")
            if frm not in wl: wl[frm] = [0, 0]
            if to not in wl: wl[to] = [0, 0]
            if fs > ts:
                wl[frm][0] += 1; wl[to][1] += 1
            elif ts > fs:
                wl[to][0] += 1; wl[frm][1] += 1
        agg = {}
        for u in users:
            agg[u["username"]] = {"quiz_n": 0, "quiz_r": 0, "cards": 0,
                                  "ck_days": 0, "ck_pts": 0, "days": []}
        for r in recs:
            a = agg.get(r["username"])
            if a is None:
                continue
            if r["kind"] == "done":
                try:
                    v = json.loads(r["val"]) if isinstance(r["val"], str) else r["val"]
                except Exception:
                    v = {}
                if isinstance(v, dict):
                    a["quiz_n"] += int(v.get("n") or 0)
                    a["quiz_r"] += int(v.get("r") or 0)
            elif r["kind"] == "known":
                a["cards"] += 1
        for c in chks:
            a = agg.get(c["username"])
            if a is None:
                continue
            a["ck_days"] += 1
            a["ck_pts"] += int(c.get("gained") or 0)
            a["days"].append(c["day"])
        rows = []
        for name, a in agg.items():
            quiz_pts = a["quiz_n"] * self.PTS_PER_QUIZ + a["quiz_r"] * self.PTS_PER_RIGHT
            card_pts = a["cards"] * self.PTS_PER_CARD
            rows.append({
                "u": _mask_name(name), "me": name == uname, "name": name,
                "score": quiz_pts + card_pts + a["ck_pts"],
                "quiz_n": a["quiz_n"], "rate": round(a["quiz_r"] / a["quiz_n"] * 100) if a["quiz_n"] else 0,
                "cards": a["cards"], "ck_days": a["ck_days"],
                "streak": self._streak(a["days"]),
                "wins": wl.get(name, [0, 0])[0], "loss": wl.get(name, [0, 0])[1],
            })
        rows.sort(key=lambda x: (-x["score"], -x["quiz_n"], x["u"]))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        me_row = next((r for r in rows if r["me"]), None)
        self.j(200, {"ok": True, "me": uname,
                     "my_rank": me_row["rank"] if me_row else None,
                     "rules": {"quiz": self.PTS_PER_QUIZ, "right": self.PTS_PER_RIGHT,
                               "card": self.PTS_PER_CARD, "checkin": self.CHECKIN_BASE,
                               "streak_step": self.CHECKIN_STREAK_STEP,
                               "streak_max": self.CHECKIN_STREAK_MAX},
                     "list": rows[:50], "total": len(rows)})

    # ---------- 好友 / 私信 / 刷新挑战（云端社交） ----------
    def _scores_for(self, names):
        """复用排行榜计分逻辑，仅对给定用户名聚合，返回 {name: stats}。"""
        recs = turso("SELECT username,kind,val FROM zb_records")
        chks = turso("SELECT username,day,gained FROM zb_checkin")
        agg = {n: {"quiz_n": 0, "quiz_r": 0, "cards": 0, "ck_days": 0,
                   "ck_pts": 0, "days": []} for n in names}
        for r in recs:
            a = agg.get(r["username"])
            if a is None:
                continue
            if r["kind"] == "done":
                try:
                    v = json.loads(r["val"]) if isinstance(r["val"], str) else r["val"]
                except Exception:
                    v = {}
                if isinstance(v, dict):
                    a["quiz_n"] += int(v.get("n") or 0)
                    a["quiz_r"] += int(v.get("r") or 0)
            elif r["kind"] == "known":
                a["cards"] += 1
        for c in chks:
            a = agg.get(c["username"])
            if a is None:
                continue
            a["ck_days"] += 1
            a["ck_pts"] += int(c.get("gained") or 0)
            a["days"].append(c["day"])
        out = {}
        for n, a in agg.items():
            quiz_pts = a["quiz_n"] * self.PTS_PER_QUIZ + a["quiz_r"] * self.PTS_PER_RIGHT
            card_pts = a["cards"] * self.PTS_PER_CARD
            out[n] = {"u": _mask_name(n), "score": quiz_pts + card_pts + a["ck_pts"],
                      "quiz_n": a["quiz_n"],
                      "rate": round(a["quiz_r"] / a["quiz_n"] * 100) if a["quiz_n"] else 0,
                      "cards": a["cards"], "ck_days": a["ck_days"],
                      "streak": self._streak(a["days"])}
        return out

    def api_friends(self):
        me = self._rec_user()
        if not me:
            return
        # 性能要点（轮询高频调用，必须尽量不查库）：
        #   ① 好友关系 + 积分榜：30 秒短缓存（社交关系变化很慢）；
        #   ② 未读数：版本号缓存（无新消息则完全不查库）。
        # 这样 45 秒一次的未读轮询基本是纯内存响应，数据库压力降一个数量级。
        cached = _FRIENDS_CACHE.get(me)
        if cached and time.time() - cached[0] < 30:
            base = cached[1]
            unread, _c = unread_get(me)
            flist = []
            for r in base["friends"]:
                row = dict(r); row["unread"] = unread.get(r["name"], 0); flist.append(row)
            flist.sort(key=lambda x: (-x.get("score", 0), -x.get("quiz_n", 0)))
            for i, r in enumerate(flist):
                r["rank"] = i + 1
            return self.j(200, {"ok": True, "friends": flist,
                                "incoming": base["incoming"], "outgoing": base["outgoing"]})
        # 好友/申请/未读原本 3 次串行往返 → 合并成 1 次 pipeline
        friends, incoming, outgoing = turso_multi([
            ("SELECT b FROM zb_friends WHERE a=? AND status='friend'", [(me, "text")]),
            ("SELECT a,ts FROM zb_friends WHERE b=? AND status='pending'", [(me, "text")]),
            ("SELECT b,ts FROM zb_friends WHERE a=? AND status='pending'", [(me, "text")]),
        ])
        fnames = [r["b"] for r in friends]
        stats = self._scores_for(fnames) if fnames else {}
        flist = []
        for n in fnames:
            s = stats.get(n, {})
            row = dict(s); row["u"] = _mask_name(n); row["name"] = n; row["me"] = False
            flist.append(row)
        flist.sort(key=lambda x: (-x.get("score", 0), -x.get("quiz_n", 0)))
        for i, r in enumerate(flist):
            r["rank"] = i + 1
        inc_out = {"incoming": [{"u": _mask_name(r["a"]), "name": r["a"], "ts": r.get("ts")} for r in incoming],
                   "outgoing": [{"u": _mask_name(r["b"]), "name": r["b"], "ts": r.get("ts")} for r in outgoing]}
        _FRIENDS_CACHE[me] = (time.time(), {"friends": [dict(r) for r in flist], "incoming": inc_out["incoming"], "outgoing": inc_out["outgoing"]})
        unread, _changed = unread_get(me)
        flist2 = []
        for r in flist:
            row = dict(r); row["unread"] = unread.get(r["name"], 0); flist2.append(row)
        return self.j(200, {"ok": True, "friends": flist2,
                            "incoming": inc_out["incoming"], "outgoing": inc_out["outgoing"]})

    def api_friend_request(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); to = (d.get("to") or "").strip()
        if not to or to == me:
            return self.j(400, {"msg": "无效的对方账号"})
        if not get_user(to):
            return self.j(404, {"msg": "该用户不存在"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ex = turso("SELECT status FROM zb_friends WHERE a=? AND b=?", [(me, "text"), (to, "text")])
        if ex and ex[0].get("status") == "friend":
            return self.j(200, {"ok": True, "already": True, "msg": "已是好友"})
        if ex and ex[0].get("status") == "pending":
            return self.j(200, {"ok": True, "already": True, "msg": "请求已发送"})
        rev = turso("SELECT status FROM zb_friends WHERE a=? AND b=?", [(to, "text"), (me, "text")])
        if rev and rev[0].get("status") == "pending":
            turso("UPDATE zb_friends SET status='friend',ts=? WHERE a=? AND b=?",
                  [(ts, "text"), (to, "text"), (me, "text")])
            _friends_cache_drop(me); _friends_cache_drop(to)
            turso("INSERT INTO zb_friends (a,b,status,ts) VALUES (?,?,?,?) "
                  "ON CONFLICT(a,b) DO UPDATE SET status='friend',ts=excluded.ts",
                  [(me, "text"), (to, "text"), ("friend", "text"), (ts, "text")])
            return self.j(200, {"ok": True, "mutual": True, "msg": "已互为好友"})
        turso("INSERT INTO zb_friends (a,b,status,ts) VALUES (?,?,?,?) "
              "ON CONFLICT(a,b) DO UPDATE SET status='pending',ts=excluded.ts",
              [(me, "text"), (to, "text"), ("pending", "text"), (ts, "text")])
        _friends_cache_drop(me); _friends_cache_drop(to)
        self.j(200, {"ok": True, "msg": "好友请求已发送"})

    def api_friend_accept(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); fr = (d.get("from") or "").strip()
        if not fr:
            return self.j(400, {"msg": "缺少对方"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ex = turso("SELECT status FROM zb_friends WHERE a=? AND b=?", [(fr, "text"), (me, "text")])
        if not ex or ex[0].get("status") != "pending":
            return self.j(404, {"msg": "没有待处理请求"})
        turso("UPDATE zb_friends SET status='friend',ts=? WHERE a=? AND b=?",
              [(ts, "text"), (fr, "text"), (me, "text")])
        turso("INSERT INTO zb_friends (a,b,status,ts) VALUES (?,?,?,?) "
              "ON CONFLICT(a,b) DO UPDATE SET status='friend',ts=excluded.ts",
              [(me, "text"), (fr, "text"), ("friend", "text"), (ts, "text")])
        _friends_cache_drop(me); _friends_cache_drop(fr)
        self.j(200, {"ok": True, "msg": "已添加为好友"})

    def api_friend_reject(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); fr = (d.get("from") or "").strip()
        if not fr:
            return self.j(400, {"msg": "缺少对方"})
        turso("DELETE FROM zb_friends WHERE a=? AND b=?", [(fr, "text"), (me, "text")])
        _friends_cache_drop(me); _friends_cache_drop(fr)
        self.j(200, {"ok": True, "msg": "已拒绝"})

    def api_msg_send(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); to = (d.get("to") or "").strip(); text = (d.get("text") or "").strip()
        if not to or not text:
            return self.j(400, {"msg": "缺少对方或内容"})
        if not get_user(to):
            return self.j(404, {"msg": "该用户不存在"})
        fr = turso("SELECT status FROM zb_friends WHERE a=? AND b=? AND status='friend'",
                   [(me, "text"), (to, "text")])
        if not fr:
            return self.j(403, {"msg": "请先加为好友再私信"})
        if len(text) > 500:
            text = text[:500]
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("INSERT INTO zb_msgs (frm,to_u,text,ts,read) VALUES (?,?,?,?,0)",
              [(me, "text"), (to, "text"), (text, "text"), (ts, "text")])
        # 通知接收方：有新消息 → 版本号 +1（内存操作，轮询时零 DB 即可感知）
        unread_bump(to)
        self.j(200, {"ok": True, "ts": ts})

    def api_msg_get(self, with_u, since=None):
        me = self._rec_user()
        if not me:
            return
        if not with_u:
            return self.j(400, {"msg": "缺少对方"})
        # 性能：支持增量拉取（since = 上次最后一条的 id）。
        # 原实现每次轮询都全量取回 + 无条件 UPDATE 标记已读（一次轮询 2 次 DB 往返），
        # 现改为：只取 id > since 的新消息；且仅当确有新消息时才执行标记已读。
        if since is not None:
            rows = turso("SELECT id,frm,to_u,text,ts,read FROM zb_msgs "
                         "WHERE id>? AND ((frm=? AND to_u=?) OR (frm=? AND to_u=?)) ORDER BY id",
                         [(int(since), "int"), (me, "text"), (with_u, "text"),
                          (with_u, "text"), (me, "text")])
        else:
            rows = turso("SELECT id,frm,to_u,text,ts,read FROM zb_msgs "
                         "WHERE (frm=? AND to_u=?) OR (frm=? AND to_u=?) ORDER BY id",
                         [(me, "text"), (with_u, "text"), (with_u, "text"), (me, "text")])
        # 标记已读（全量与增量都要）——
        # 原实现只在增量分支标记，全量拉取不标记，导致 /api/friends 的 unread 统计一直有值，
        # 前端轮询回来数字又冒出来（红框反复闪）。这里无条件标记。
        try:
            turso("UPDATE zb_msgs SET read=1 WHERE to_u=? AND frm=? AND read=0",
                  [(me, "text"), (with_u, "text")])
        except Exception:
            pass
        # 已读状态变了 → 未读缓存失效，下次轮询会重新计算
        unread_drop(me)
        last_id = None
        for r in rows:
            try: last_id = int(r.get("id"))
            except Exception: pass
        self.j(200, {"ok": True, "with": with_u, "last_id": last_id,
                    "list": [{"id": r.get("id"), "frm": r["frm"], "to": r["to_u"],
                              "text": r["text"], "ts": r["ts"],
                              "mine": r["frm"] == me} for r in rows]})

    def api_challenge_new(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); to = (d.get("to") or "").strip()
        if not to or to == me:
            return self.j(400, {"msg": "无效的对方"})
        if not get_user(to):
            return self.j(404, {"msg": "该用户不存在"})
        fr = turso("SELECT status FROM zb_friends WHERE a=? AND b=? AND status='friend'",
                   [(me, "text"), (to, "text")])
        if not fr:
            return self.j(403, {"msg": "请先加为好友再发起挑战"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # Turso HTTP 管线下 last_insert_rowid 不可靠（每次调用独立连接），改用毫秒时间戳显式生成 id
        cid = int(time.time() * 1000)
        turso("INSERT INTO zb_challenges (id,frm,to_u,status,ts) VALUES (?,?,?,?,?)",
              [(cid, "int"), (me, "text"), (to, "text"), ("pending", "text"), (ts, "text")])
        self.j(200, {"ok": True, "id": cid, "msg": "挑战已发起，等好友应战"})

    def api_challenge_result(self):
        me = self._rec_user()
        if not me:
            return
        d = self.body_json(); cid = d.get("id"); score = d.get("score")
        try:
            score = int(score)
        except Exception:
            score = 0
        if not cid:
            return self.j(400, {"msg": "缺少挑战 id"})
        ch = turso("SELECT * FROM zb_challenges WHERE id=?", [(cid, "int")])
        if not ch:
            return self.j(404, {"msg": "挑战不存在"})
        ch = ch[0]
        if me == ch["frm"]:
            turso("UPDATE zb_challenges SET frm_score=? WHERE id=?", [(score, "int"), (cid, "int")])
        elif me == ch["to"]:
            turso("UPDATE zb_challenges SET to_score=? WHERE id=?", [(score, "int"), (cid, "int")])
        else:
            return self.j(403, {"msg": "无权提交此挑战"})
        ch = turso("SELECT * FROM zb_challenges WHERE id=?", [(cid, "int")])[0]
        if ch.get("frm_score") is not None and ch.get("to_score") is not None:
            turso("UPDATE zb_challenges SET status='done' WHERE id=?", [(cid, "int")])
        self.j(200, {"ok": True, "msg": "成绩已记录"})

    def api_challenge_get(self):
        me = self._rec_user()
        if not me:
            return
        rows = turso("SELECT id,frm,to_u,status,frm_score,to_score,ts FROM zb_challenges "
                     "WHERE frm=? OR to_u=? ORDER BY id DESC LIMIT 30", [(me, "text"), (me, "text")])
        out = [{"id": r["id"], "frm": _mask_name(r["frm"]), "frm_name": r["frm"],
                "to": _mask_name(r["to"]), "to_name": r["to"], "status": r["status"],
                "frm_score": r.get("frm_score"), "to_score": r.get("to_score"), "ts": r.get("ts"),
                "mine": True} for r in rows]
        self.j(200, {"ok": True, "list": out})

    # ---------- 背诵语音过关（服务端二次校验，防刷分） ----------
    # 只有真实背诵（转文字后与答案相似度达标）才会写入 kind='known'，
    # 从而计入排行榜的「背诵过关 +5 分」；手动点按钮不再直接给分。
    RECITE_PASS = 0.55

    def api_recite(self):
        uname = self._rec_user()
        if not uname:
            return
        d = self.body_json()
        key = (d.get("key") or "").strip()
        transcript = (d.get("transcript") or "").strip()
        if not key or not transcript:
            return self.j(400, {"msg": "缺少 key 或 transcript"})
        card = _load_card_index().get(key)
        if not card:
            return self.j(404, {"msg": "未找到对应卡片，请刷新资料后重试"})
        score = text_sim(transcript, card.get("back") or "")
        passed = score >= self.RECITE_PASS
        if passed:
            val = int(round(score * 100))
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            turso("INSERT INTO zb_records (username,kind,key,val,updated) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(username,kind,key) DO UPDATE SET val=excluded.val, updated=excluded.updated",
                  [(uname, "text"), ("known", "text"), (key, "text"), (val, "int"), (ts, "text")])
            return self.j(200, {"ok": True, "pass": True, "score": round(score, 3),
                                "msg": "背诵过关！语音测评 %d 分，+5 分" % int(score * 100)})
        return self.j(200, {"ok": True, "pass": False, "score": round(score, 3),
                            "expected": card.get("back") or "",
                            "msg": "还差一点（测评 %d 分），再听一遍答案、多读两遍试试" % int(score * 100)})

    # ---------- 下载权益（打赏解锁 / 兑换码） ----------
    DL_PACK_PATH = os.path.join(SITE_DIR, "files", "study-pack.docx")

    def _dl_user(self):
        u = self.cur_user()
        if not u:
            self.j(401, {"code": "LOGIN", "msg": "请先登录"})
            return None
        if (u.get("status") or "") != "approved":
            self.j(403, {"code": "PENDING", "msg": "账号待审核"})
            return None
        return u["username"]

    def api_dl_status(self):
        uname = self._dl_user()
        if not uname:
            return
        granted = has_dl(uname) or member_active(self.cur_user())
        req = None
        rows = turso("SELECT code,created_at,status FROM zb_dl_req WHERE username=?", [(uname, "text")])
        if rows and (rows[0].get("status") or "pending") == "pending":
            req = {"code": rows[0].get("code"), "created_at": rows[0].get("created_at") or ""}
        return self.j(200, {"ok": True, "granted": granted, "req": req})

    def api_dl_request(self):
        uname = self._dl_user()
        if not uname:
            return
        rows = turso("SELECT code,status FROM zb_dl_req WHERE username=?", [(uname, "text")])
        if rows and (rows[0].get("status") or "pending") == "pending":
            return self.j(200, {"ok": True, "code": rows[0].get("code"), "msg": "已有待处理的申请"})
        code = "%04d" % secrets.randbelow(10000)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("INSERT INTO zb_dl_req (username,code,status,created_at,updated_at) VALUES (?,?,'pending',?,?) "
              "ON CONFLICT(username) DO UPDATE SET code=excluded.code, status='pending', "
              "created_at=excluded.created_at, updated_at=excluded.updated_at",
              [(uname, "text"), (code, "text"), (ts, "text"), (ts, "text")])
        return self.j(200, {"ok": True, "code": code, "msg": "申请已提交"})

    def api_dl_redeem(self):
        uname = self._dl_user()
        if not uname:
            return
        ip = self.client_address[0]
        if too_many(ip + "|dl"):
            return self.j(429, {"msg": "尝试次数过多，请 10 分钟后再试"})
        d = self.body_json()
        code = (d.get("code") or "").strip().upper().replace(" ", "")
        if not code:
            return self.j(400, {"msg": "请输入兑换码"})
        rows = turso("SELECT code,used_by FROM zb_dl_codes WHERE code=?", [(code, "text")])
        if not rows:
            mark_fail(ip + "|dl")
            return self.j(404, {"msg": "兑换码不存在，请核对后重试"})
        if rows[0].get("used_by"):
            return self.j(409, {"msg": "该兑换码已被使用"})
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        turso("UPDATE zb_dl_codes SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
              [(uname, "text"), (ts, "text"), (code, "text")])
        turso("INSERT INTO zb_dl (username,source,granted_at) VALUES (?, 'code', ?) "
              "ON CONFLICT(username) DO NOTHING", [(uname, "text"), (ts, "text")])
        clear_fail(ip + "|dl")
        return self.j(200, {"ok": True, "granted": True, "msg": "兑换成功！资料包已解锁"})

    def serve_download_package(self):
        return self.serve_download_file()

    def _dl_gate(self):
        u = self.cur_user()
        if not u:
            self.j(401, {"code": "LOGIN", "msg": "请先登录"})
            return None
        if (u.get("status") or "") != "approved":
            self.j(403, {"code": "PENDING", "msg": "账号待审核"})
            return None
        if not (has_dl(u["username"]) or member_active(u)):
            self.j(402, {"code": "DL", "msg": "下载权益未开通：请先打赏或使用兑换码（会员自动含下载）"})
            return None
        return u

    def _dl_dir(self):
        return os.path.join(SITE_DIR, "files", "dl")

    def api_dl_files(self):
        # 清单浏览：仅登录即可（免费账号可看「有哪些资料」），实际下载仍由 _dl_gate 拦截
        u = self.cur_user()
        if not u:
            self.j(401, {"code": "LOGIN", "msg": "请先登录"})
            return
        out = []
        for f in DL_FILES:
            p = os.path.join(self._dl_dir(), f["file"])
            out.append({"id": f["id"], "name": f["name"], "subject": f["subject"],
                        "desc": f.get("desc") or "",
                        "size": os.path.getsize(p) if os.path.exists(p) else 0})
        return self.j(200, {"ok": True, "files": out})

    def serve_download_file(self):
        u = self._dl_gate()
        if not u:
            return
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        fid = (qs.get("id") or [""])[0]
        meta = next((f for f in DL_FILES if f["id"] == fid), None)
        if not meta:
            return self.j(404, {"msg": "未找到该资料"})
        p = os.path.join(self._dl_dir(), meta["file"])
        if not os.path.exists(p):
            return self.j(404, {"msg": "文件未就绪，请联系管理员"})
        with open(p, "rb") as fh:
            data = fh.read()
        extra = {"Content-Disposition": "attachment; filename=\"study.docx\"; filename*=UTF-8''%s"
                 % quote(meta["file"]),
                 "Cache-Control": "no-store"}
        self._send(200, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", extra)

    def _build_print_html(self, subject):
        """生成某学科「精华+导图+卡片」打印版 HTML；无资料返回 None"""
        try:
            with open(CONTENT_PATH, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except Exception:
            return None
        vols = (data.get("subjects") or {}).get(subject) or {}
        if not vols:
            return None
        cards = [c for c in (data.get("cards") or []) if (c.get("subject") or "") == subject]
        h = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>', esc_html(subject), ' 学科精华打印版 · 52ZX吾爱自学</title>',
             '<style>', PRINT_CSS, '</style></head><body>',
             '<div class="toolbar"><button onclick="window.print()">🖨️ 打印 / 另存为 PDF</button>',
             '<span>打印设置里勾选「背景图形」，导图配色更完整</span></div>',
             '<h1 class="cover">', esc_html(subject), ' · 学科精华（打印版）</h1>',
             '<div class="meta">52ZX 吾爱自学 · 生成于 ', time.strftime('%Y-%m-%d'), '</div>',
             '<div class="src">📄 资料来源：', esc_html(DL_SRC.get(subject, '教材同步整理')),
             '<br>本打印版与网站「资料」页同源：一=学习资料，二=思维导图，其后=赋分/必背/必修卡片。</div>']
        for vol, leaf in vols.items():
            h.append('<h2>' + esc_html(subject) + ' · ' + esc_html(vol) + '</h2>')
            h.append('<h3>一、学习资料（全文总结）</h3><div class="sec">' + (leaf.get("summary") or "") + '</div>')
            h.append('<h3>二、思维导图</h3><div class="sec mm">' + mm_elbow(leaf.get("mindmap") or "") + '</div>')
            cs = [c for c in cards if (c.get("volume") or "") == vol]
            sec_no = 3
            for tag in ("赋分", "必背", "必修", "重难"):
                tc = [c for c in cs if (c.get("tag") or "") == tag]
                if not tc:
                    continue
                cn = "三四五六"[sec_no - 3] if sec_no <= 6 else str(sec_no)
                h.append('<h3>' + cn + '、' + esc_html(tag) + '卡片（' + str(len(tc)) + ' 条）</h3>')
                for c in tc:
                    star = ' <span class="star">★</span>' if c.get("star") else ''
                    h.append('<div class="kcard"><div class="fr">' + esc_html(c.get("front")) + star +
                             '</div><div class="bk">' + esc_html(c.get("back")) + '</div></div>')
                sec_no += 1
        if data.get("memory"):
            h.append('<h2>附录 · 高效记忆八法与学科专属策略</h2><div class="sec">' + data["memory"] + '</div>')
        h.append('</body></html>')
        return "".join(h)

    def api_dl_print(self):
        u = self._dl_gate()
        if not u:
            return
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        subject = (qs.get("subject") or [""])[0]
        html = self._build_print_html(subject)
        if html is None:
            return self.j(404, {"msg": "未找到该科目资料"})
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def serve_download_zip(self):
        u = self._dl_gate()
        if not u:
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in DL_FILES:
                p = os.path.join(self._dl_dir(), f["file"])
                if os.path.exists(p):
                    z.write(p, "52ZX资料包/" + f["file"])
            for s in ("政治", "历史", "地理", "生物"):
                html = self._build_print_html(s)
                if html:
                    z.writestr("52ZX资料包/打印版（浏览器打开→另存为PDF）/%s精华打印版.html" % s, html)
            z.writestr("52ZX资料包/说明.txt",
                       "52ZX 吾爱自学 · 资料包（生成于 %s）\n\n"
                       "1. docx 直接打印；\n2. 「打印版」文件夹里的 HTML 用浏览器打开，点右上角按钮或 Ctrl+P，「另存为 PDF」即得思维导图 PDF；\n"
                       "3. 全部内容与网站「资料」页同源：学习资料/思维导图/赋分原句/知识点卡片。\n" % time.strftime("%Y-%m-%d"))
        data = buf.getvalue()
        extra = {"Content-Disposition": "attachment; filename=\"52ZX-pack.zip\"; filename*=UTF-8''%s"
                 % quote("52ZX资料包-%s.zip" % time.strftime("%Y%m%d")),
                 "Cache-Control": "no-store"}
        self._send(200, data, "application/zip", extra)

    def api_admin_dl(self):
        if not self._need_admin():
            return
        reqs = turso("SELECT username,code,status,created_at FROM zb_dl_req ORDER BY created_at DESC LIMIT 100")
        codes = turso("SELECT code,used_by,used_at,created_at FROM zb_dl_codes ORDER BY created_at DESC LIMIT 200")
        grants = turso("SELECT username,source,granted_at FROM zb_dl ORDER BY granted_at DESC LIMIT 200")
        return self.j(200, {"ok": True,
                            "requests": [{"username": r["username"], "code": r.get("code"),
                                          "status": r.get("status") or "pending",
                                          "created_at": r.get("created_at") or ""} for r in reqs],
                            "codes": [{"code": c["code"], "used_by": c.get("used_by") or "",
                                       "used_at": c.get("used_at") or ""} for c in codes],
                            "grants": [{"username": g["username"], "source": g.get("source") or "admin",
                                        "granted_at": g.get("granted_at") or ""} for g in grants]})

    def api_admin_dl_act(self):
        if not self._need_admin():
            return
        d = self.body_json()
        a = (d.get("act") or "").strip()
        u = (d.get("u") or "").strip()
        admin_audit("dl_act:" + a, u, self.client_ip(), "")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if a == "grant":
            if not u:
                return self.j(400, {"msg": "缺少用户名"})
            turso("INSERT INTO zb_dl (username,source,granted_at) VALUES (?, 'admin', ?) "
                  "ON CONFLICT(username) DO NOTHING", [(u, "text"), (ts, "text")])
            turso("UPDATE zb_dl_req SET status='done', updated_at=? WHERE username=?", [(ts, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已开通 " + u + " 的下载权益"})
        if a == "revoke":
            if not u:
                return self.j(400, {"msg": "缺少用户名"})
            turso("DELETE FROM zb_dl WHERE username=?", [(u, "text")])
            return self.j(200, {"ok": True, "msg": "已取消 " + u + " 的下载权益"})
        if a == "member_all":
            # 打赏/兑换码只给下载权限；学生另付 ¥9.9/¥16.9 开通会员时，管理员在这里手动开
            if not u:
                return self.j(400, {"msg": "缺少用户名"})
            if not get_user(u):
                return self.j(404, {"msg": "用户不存在"})
            year = time.strftime("%Y-%m-%d", time.localtime(time.time() + 365 * 86400))
            turso("UPDATE zb_users SET member=1, status='approved', member_tier='all', member_grade='', "
                  "member_grades='高一,高二,高三', expire=? WHERE username=?", [(year, "text"), (u, "text")])
            # 会员自动含下载权益，顺手把该账号的打赏申请标记为已处理
            turso("UPDATE zb_dl_req SET status='done', updated_at=? WHERE username=?", [(ts, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已为 " + u + " 开通全三册会员（高一/高二/高三）至 " + year})
        if a == "member_grade":
            if not u:
                return self.j(400, {"msg": "缺少用户名"})
            g = (d.get("g") or "").strip()
            if g not in ("高一", "高二", "高三"):
                return self.j(400, {"msg": "年级无效，须为 高一/高二/高三"})
            if not get_user(u):
                return self.j(404, {"msg": "用户不存在"})
            year = time.strftime("%Y-%m-%d", time.localtime(time.time() + 365 * 86400))
            turso("UPDATE zb_users SET member=1, status='approved', member_tier='single', member_grade='', "
                  "member_grades=?, expire=? WHERE username=?", [(g, "text"), (year, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已为 " + u + " 单独开通「" + g + "」年级会员至 " + year})
        if a == "delreq":
            if not u:
                return self.j(400, {"msg": "缺少用户名"})
            turso("DELETE FROM zb_dl_req WHERE username=?", [(u, "text")])
            return self.j(200, {"ok": True, "msg": "已删除该申请"})
        if a == "gencodes":
            try:
                n = max(1, min(100, int(d.get("n") or 10)))
            except Exception:
                n = 10
            alpha = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
            made = []
            for _ in range(n):
                for _try in range(5):
                    c = "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(2))
                    try:
                        turso("INSERT INTO zb_dl_codes (code,created_at) VALUES (?,?)", [(c, "text"), (ts, "text")])
                        made.append(c)
                        break
                    except TursoError as e:
                        if "UNIQUE" in str(e).upper():
                            continue
                        raise
            return self.j(200, {"ok": True, "codes": made, "msg": "已生成 %d 个兑换码" % len(made)})
        return self.j(400, {"msg": "未知操作"})

    # ---------- 邀请裂变：阶梯奖励（防刷 + 有效性判定） ----------
    # 阶梯：
    #   3 位有效邀请 → 资料下载权限（一次性激活码）
    #  10 位有效邀请 → 自动激活「一个年级」的会员权限
    #  20 位有效邀请 → 自动激活「三年」的全三册会员权限
    # 有效邀请的判定（防僵尸号）：
    #   ① 已激活：被邀请人至少登录过一次（有 last_login）；
    #   ② 有使用痕迹：在 zb_records 里有学习记录（答题/背诵/错题等）；
    #   ③ 付费过半：有效邀请中"已付费"（会员或已解锁下载）的人数 ≥ 一半。
    # 不满足①②的算"无效邀请"（僵尸号），不计入阶梯进度。
    INVITE_NEED = 3              # 下载权限
    INVITE_NEED_MEMBER = 10      # 一个年级会员
    INVITE_NEED_YEARS = 20       # 三年全三册
    INVITE_YEARS = 3             # 三年

    def _invite_rows(self, uname):
        return turso("SELECT invitee,created_at,ip,fp,activated,acted,paid,valid,reason "
                     "FROM zb_invites WHERE inviter=? ORDER BY created_at DESC", [(uname, "text")])

    def _refresh_invite_validity(self, uname):
        """重算某邀请人的有效邀请数（僵尸号剔除）。

        判定：① 登录过（激活）② 有学习记录（使用痕迹）。
        另外统计"已付费"人数（会员 或 已解锁下载）。
        """
        rows = self._invite_rows(uname)
        if not rows:
            return {"total": 0, "valid": 0, "activated": 0, "acted": 0, "paid": 0, "paid_ok": False,
                    "tiers": [], "detail": []}
        names = [r["invitee"] for r in rows]
        # 批量取这些用户的登录/会员/下载状态与学习记录数
        ph = ",".join("?" * len(names))
        urows = turso("SELECT username,last_login,member,expire FROM zb_users WHERE username IN (%s)" % ph,
                      [(n, "text") for n in names])
        umap = {r["username"]: r for r in urows}
        dlrows = turso("SELECT username FROM zb_dl WHERE username IN (%s)" % ph,
                       [(n, "text") for n in names])
        dlset = {r["username"] for r in dlrows}
        cntrows = turso("SELECT username,COUNT(*) AS c FROM zb_records WHERE username IN (%s) "
                        "GROUP BY username" % ph, [(n, "text") for n in names])
        cmap = {r["username"]: int(r["c"]) for r in cntrows}

        detail = []
        n_act = n_acted = n_paid = n_valid = 0
        for r in rows:
            name = r["invitee"]
            u = umap.get(name) or {}
            # 已激活：登录接口会写 zb_invites.activated=1；老数据回退看 last_login
            activated = 1 if (r.get("activated") or (u.get("last_login") or "")) else 0
            acted = 1 if cmap.get(name, 0) > 0 else 0
            paid = 1 if (str(u.get("member") or "") in ("1", "true", "True")) or name in dlset else 0
            # 有效 = 已激活 且 有使用痕迹
            valid = 1 if (activated and acted) else 0
            reason = ""
            if not activated:
                reason = "未激活（注册后未登录）"
            elif not acted:
                reason = "无使用痕迹（未学习）"
            n_act += activated
            n_acted += acted
            n_paid += paid
            n_valid += valid
            detail.append({"name": name, "activated": activated, "acted": acted,
                           "paid": paid, "valid": valid, "reason": reason,
                           "created_at": r.get("created_at") or ""})
        # 付费过半才算通过（针对"有效邀请"这个集合）
        paid_ok = (n_valid > 0 and n_paid * 2 >= n_valid)
        # 回写状态（便于后台查询与前端展示）
        try:
            for d in detail:
                turso("UPDATE zb_invites SET activated=?,acted=?,paid=?,valid=?,reason=? WHERE invitee=?",
                      [(d["activated"], "int"), (d["acted"], "int"), (d["paid"], "int"),
                       (d["valid"], "int"), (d["reason"] or "", "text"), (d["name"], "text")])
        except Exception:
            pass
        return {"total": len(rows), "valid": n_valid, "activated": n_act, "acted": n_acted,
                "paid": n_paid, "paid_ok": paid_ok, "detail": detail}

    def _tiers(self):
        return [{"key": "dl", "need": self.INVITE_NEED, "label": "资料下载权限",
                 "desc": "一次性激活码，解锁资料下载"},
                {"key": "member1", "need": self.INVITE_NEED_MEMBER, "label": "一个年级会员",
                 "desc": "自动开通一个年级的题库会员权限"},
                {"key": "years3", "need": self.INVITE_NEED_YEARS, "label": "三年全三册会员",
                 "desc": "自动开通三年内的高中全三册会员权限"}]

    def _grant_member_years(self, uname, years):
        """开通 N 年全三册会员（到期时间 = 今天 + N 年）。"""
        base = time.strftime("%Y-%m-%d")
        try:
            cur = time.strptime(base, "%Y-%m-%d")
        except Exception:
            cur = time.localtime()
        end = time.strftime("%Y-%m-%d", time.localtime(time.mktime(cur) + 365 * 86400 * years))
        turso("UPDATE zb_users SET member=1, status='approved', member_tier='all', member_grade='', "
              "member_grades='高一,高二,高三', expire=?, approved_at=? WHERE username=?",
              [(end, "text"), (time.strftime("%Y-%m-%d %H:%M"), "text"), (uname, "text")])
        return end

    def _grant_member_grade(self, uname, grade):
        """开通一个年级的会员（默认当前年级）。"""
        grade = grade or "高一"
        if grade not in VALID_GRADES:
            grade = "高一"
        try:
            cur = time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")
            end = time.strftime("%Y-%m-%d", time.localtime(time.mktime(cur) + 365 * 86400))
        except Exception:
            end = ""
        turso("UPDATE zb_users SET member=1, status='approved', member_tier='single', member_grade=?, "
              "member_grades=?, expire=?, approved_at=? WHERE username=?",
              [(grade, "text"), (grade, "text"), (end, "text"),
               (time.strftime("%Y-%m-%d %H:%M"), "text"), (uname, "text")])
        return end

    def _gen_dl_code(self):
        """生成一次性资料下载激活码。"""
        alpha = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for _ in range(5):
            c = "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(2))
            try:
                turso("INSERT INTO zb_dl_codes (code,created_at) VALUES (?,?)", [(c, "text"), (ts, "text")])
                return c
            except TursoError as e:
                if "UNIQUE" in str(e).upper():
                    continue
                raise
        return ""

    def _auto_grant(self, uname, stat):
        """按阶梯自动发放奖励（幂等：已发过的不重复发）。

        返回发放记录列表。
        """
        got = []
        valid = stat["valid"]
        paid_ok = stat["paid_ok"]
        if not paid_ok:
            return got
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        def _granted(tier):
            r = turso("SELECT granted_at FROM zb_invite_grants WHERE username=? AND tier=?",
                      [(uname, "text"), (tier, "text")])
            return bool(r)
        def _mark(tier, note):
            try:
                turso("INSERT INTO zb_invite_grants (username,tier,granted_at,note) VALUES (?,?,?,?)",
                      [(uname, "text"), (tier, "text"), (ts, "text"), (note[:180], "text")])
            except Exception:
                pass
        # ① 下载权限（3 人）
        if valid >= self.INVITE_NEED and not _granted("dl"):
            try:
                marker = "INV-DL-" + str(int(time.time()))
                turso("INSERT INTO zb_invite_reward (username,code,claimed_at) VALUES (?,?,?)",
                      [(uname, "text"), (marker, "text"), (ts, "text")])
                code = self._gen_dl_code()
                turso("UPDATE zb_invite_reward SET code=? WHERE username=? AND code=?",
                      [(code, "text"), (uname, "text"), (marker, "text")])
                _mark("dl", "资料下载激活码 " + code)
                got.append({"tier": "dl", "msg": "已发放资料下载激活码：%s" % code, "code": code})
            except Exception:
                pass
        # ② 一个年级会员（10 人）
        if valid >= self.INVITE_NEED_MEMBER and not _granted("member1"):
            try:
                cur = get_user(uname) or {}
                g = (cur.get("member_grade") or "").strip()
                if g not in VALID_GRADES:
                    g = "高一"
                end = self._grant_member_grade(uname, g)
                _mark("member1", "开通 %s 年级会员至 %s" % (g, end))
                got.append({"tier": "member1", "msg": "已自动开通「%s」年级会员，有效期至 %s" % (g, end or "永久")})
            except Exception:
                pass
        # ③ 三年全三册（20 人）
        if valid >= self.INVITE_NEED_YEARS and not _granted("years3"):
            try:
                end = self._grant_member_years(uname, self.INVITE_YEARS)
                _mark("years3", "开通三年全三册会员至 %s" % end)
                got.append({"tier": "years3", "msg": "已自动开通三年全三册会员，有效期至 %s" % end})
            except Exception:
                pass
        return got

    def api_invite_claim(self):
        # 兼容旧前端：阶梯奖励已改为「达标自动发放」，此接口只触发一次发放并回传结果
        u = self.cur_user()
        if not u:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        uname = u["username"]
        stat = self._refresh_invite_validity(uname)
        granted = self._auto_grant(uname, stat)
        rows = turso("SELECT code,claimed_at FROM zb_invite_reward WHERE username=?", [(uname, "text")])
        code = rows[0]["code"] if rows else ""
        if granted:
            return self.j(200, {"ok": True, "code": code,
                                "msg": granted[0].get("msg") or "奖励已自动发放"})
        if not stat["paid_ok"]:
            return self.j(400, {"msg": "有效邀请中付费人数未过半，奖励暂不发放"})
        return self.j(400, {"msg": "尚未达到奖励门槛（当前 %d 位有效邀请）" % stat["valid"]})

    def api_invite_stat(self):
        u = self.cur_user()
        if not u:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        uname = u["username"]
        stat = self._refresh_invite_validity(uname)
        # 自动发放阶梯奖励
        granted = self._auto_grant(uname, stat)
        rows = turso("SELECT code,claimed_at FROM zb_invite_reward WHERE username=?", [(uname, "text")])
        reward = {"code": rows[0]["code"], "claimed_at": rows[0].get("claimed_at") or ""} if rows else None
        tiers = []
        for t in self._tiers():
            tiers.append({"key": t["key"], "need": t["need"], "label": t["label"], "desc": t["desc"],
                          "reached": stat["valid"] >= t["need"] and stat["paid_ok"]})
        return self.j(200, {"ok": True,
                            "invited": stat["total"], "valid": stat["valid"],
                            "activated": stat["activated"], "acted": stat["acted"],
                            "paid": stat["paid"], "paid_ok": stat["paid_ok"],
                            "need": self.INVITE_NEED, "tiers": tiers,
                            "can_claim": (stat["valid"] >= self.INVITE_NEED and stat["paid_ok"]),
                            "reward": reward, "granted": granted})

    # ---------- 内容（鉴权后下发） ----------
    def api_content(self):
        u = self.cur_user()
        if not u:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        if (u.get("status") or "") != "approved":
            return self.j(403, {"code": "PENDING", "msg": "账号待审核"})
        ip = self.client_ip()
        if content_too_many(u["username"]):
            return self.j(429, {"code": "RATE", "msg": "内容获取过于频繁，请 10 分钟后再试"})
        tok = self.headers.get("X-Content-Ticket") or ""
        if not consume_content_ticket(u["username"], tok):
            return self.j(403, {"code": "TICKET", "msg": "请先获取内容凭证"})
        if not os.path.exists(CONTENT_PATH):
            return self.j(500, {"msg": "资料未生成"})
        # 传输优化：直接发 gzip 二进制（Content-Encoding: gzip），浏览器自动解压。
        # 原方案是 gzip + base64 文本，体积凭空膨胀 33%（603KB → 804KB），
        # 且每次请求都要实时压缩一次（88ms CPU）。现用预压缩缓存，传输量降 25%、CPU 归零。
        # 防爬能力仍由「登录 + 审核 + 单次票据」三层把关保证，不依赖编码混淆。
        blob = content_gzip_blob(free=not member_active(u))
        if blob is None:
            return self.j(500, {"msg": "资料未生成"})
        content_mark(u["username"])
        log_content(u["username"], ip)
        self._send(200, blob, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store, no-cache, must-revalidate",
                    "Content-Encoding": "gzip",
                    "X-Content-Encoding": "gzip"})

    def api_content_ticket(self):
        u = self.cur_user()
        if not u:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        if (u.get("status") or "") != "approved":
            return self.j(403, {"code": "PENDING", "msg": "账号待审核"})
        ip = self.client_ip()
        if too_many(ip):
            return self.j(429, {"code": "RATE", "msg": "操作过于频繁，请稍后再试"})
        return self.j(200, {"ticket": issue_content_ticket(u["username"])})

    # ---------- 注册 ----------
    def api_register(self, ip):
        d = self.body_json()
        u = (d.get("u") or "").strip()
        p = d.get("p") or ""
        phone = (d.get("phone") or "").strip()[:40]
        note = (d.get("note") or "").strip()[:80]
        inv = (d.get("inv") or "").strip()[:20]
        if not USER_RE.match(u):
            return self.j(400, {"msg": "用户名 2-20 位，可用中文/字母/数字/下划线"})
        if len(p) < 6:
            return self.j(400, {"msg": "密码至少 6 位"})
        if get_user(u):
            return self.j(409, {"msg": "该用户名已被注册，请直接登录或换一个"})

        # ===== 防刷三道关 =====
        # ① 同 IP 每日注册上限 + ② 同设备注册上限
        fp = device_fp(self.headers.get("User-Agent") or "", self.headers.get("Accept-Language") or "")
        blocked = reg_reached(ip, fp)
        if blocked:
            return self.j(429, {"msg": blocked})
        # ③ 邀请关系校验：禁止自我邀请 / 同设备 / 同 IP 互相邀请
        invited_by = ""
        inv_reason = ""
        if inv and inv != u:
            inviter = get_user(inv)
            if inviter:
                inv_reason = invite_self_block(inviter, u, ip, fp)
                if not inv_reason:
                    invited_by = inv
        elif inv and inv == u:
            inv_reason = "不能邀请自己"

        salt = secrets.token_hex(16)
        ts = time.strftime("%Y-%m-%d %H:%M")
        turso(
            "INSERT INTO zb_users (username,salt,pwd,status,member,member_grades,phone,note,created_at,invited_by) "
            "VALUES (?,?,?,'approved',0,'',?,?,?,?)",
            [(u, "text"), (salt, "text"), (hash_pwd(p, salt), "text"),
             (phone, "text"), (note, "text"), (ts, "text"),
             (invited_by, "text")],
        )
        reg_mark(ip)
        reg_mark_fp(ip, fp)
        # 记录注册时的 IP / 设备指纹（用于后续"邀请双方是否同源"判定）
        try:
            turso("INSERT INTO zb_regmeta (username,ip,fp,ua,created_at) VALUES (?,?,?,?,?)",
                  [(u, "text"), (ip or "?", "text"), (fp, "text"),
                   (self.headers.get("User-Agent") or "")[:200], (ts, "text")])
        except Exception:
            pass
        # 记录邀请关系（含被拦下的无效邀请，便于排查）
        if inv:
            try:
                turso("INSERT INTO zb_invites (invitee,inviter,created_at,ip,fp,ua,valid,reason) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      [(u, "text"), (inv, "text"), ts, (ip or "?", "text"), (fp, "text"),
                       (self.headers.get("User-Agent") or "")[:200], 0,
                       (inv_reason or ("pending" if invited_by else "inviter_not_found"))])
            except Exception:
                pass
        msg = "注册成功！可直接登录浏览（会员功能按年级开通）"
        if inv_reason and inv_reason not in ("pending",):
            msg += "（%s，本次邀请未计入）" % inv_reason
        return self.j(200, {"ok": True, "pending": False, "msg": msg})

    # ---------- 登录 ----------
    def api_auto_login(self):
        """用长期令牌（zb_rt，HttpOnly Cookie）自动换取会话 cookie（zb_sid）。

        这是"记住我"的安全实现：前端不存密码，只在启动时带 HttpOnly Cookie 调一次本接口；
        令牌无效则返回 login:false，前端正常显示登录框。
        """
        m = self.cookies().get(REFRESH_COOKIE)
        if not m:
            return self.j(200, {"ok": False, "login": False})
        try:
            u = verify_refresh_token(m.value)
        except Exception:
            u = None
        if not u:
            return self.j(200, {"ok": False, "login": False})
        row = get_user(u)
        if not row:
            return self.j(200, {"ok": False, "login": False})
        status = row.get("status") or "pending"
        if status != "approved":
            return self.j(200, {"ok": False, "login": False, "status": status})
        act = member_active(row)
        mi = member_info(row)
        cookie = "zb_sid=%s; " % make_sid(u) + self._cookie_attrs(SESSION_HOURS * 3600)
        return self.j(200, {"ok": True, "login": True, "u": u, "member": act, "status": status,
                            "remember": True,
                            "member_tier": mi["tier"], "member_grade": mi["grade"],
                            "member_grades": mi["grades"], "expire": row.get("expire") or ""},
                       {"Set-Cookie": cookie})

    def api_change_password(self):
        """用户自行修改密码。

        安全要点：
          · 必须验证**旧密码**（防止会话被劫持后直接改密）；
          · 新密码强度校验（≥6 位）；
          · 改密后**撤销该用户全部长期登录令牌**（zb_rt）——
            若不撤销，旧的"记住我"凭据仍能免密登录，等于密码白改；
          · 改密后**重新签发会话与长期令牌**，用户无需重新登录。
        """
        me = self.cur_user()
        if not me:
            return self.j(401, {"code": "LOGIN", "msg": "请先登录"})
        uname = me["username"]
        d = self.body_json()
        old_p = d.get("old") or ""
        new_p = d.get("new") or ""
        if not old_p or not new_p:
            return self.j(400, {"msg": "请填写原密码和新密码"})
        if len(new_p) < 6:
            return self.j(400, {"msg": "新密码至少 6 位"})
        if old_p == new_p:
            return self.j(400, {"msg": "新密码不能与原密码相同"})
        row = get_user(uname)
        if not row:
            return self.j(404, {"msg": "账号不存在"})
        if not hmac.compare_digest(row["pwd"], hash_pwd(old_p, row["salt"])):
            return self.j(401, {"msg": "原密码不正确"})
        salt = secrets.token_hex(16)
        turso("UPDATE zb_users SET salt=?, pwd=? WHERE username=?",
              [(salt, "text"), (hash_pwd(new_p, salt), "text"), (uname, "text")])
        # 改密后旧的长期令牌全部作废（安全要求），再重新签发
        revoke_refresh_tokens(uname)
        try:
            tok, _exp = issue_refresh_token(uname)
            cookie = self._cookie_multi([("zb_sid", make_sid(uname), SESSION_HOURS * 3600),
                                         (REFRESH_COOKIE, tok, REFRESH_DAYS * 86400)])
        except Exception:
            cookie = self._cookie_multi([("zb_sid", make_sid(uname), SESSION_HOURS * 3600)])
        return self.j(200, {"ok": True, "msg": "密码修改成功"}, {"Set-Cookie": cookie})

    def api_login(self, ip):
        if too_many(ip):
            return self.j(429, {"msg": "尝试次数过多，请 10 分钟后再试"})
        d = self.body_json()
        u = (d.get("u") or "").strip()
        p = d.get("p") or ""
        if not u or not p:
            return self.j(400, {"msg": "请输入用户名和密码"})
        row = get_user(u)
        if not row:
            mark_fail(ip)
            return self.j(404, {"msg": "账号不存在，请先注册"})
        if not hmac.compare_digest(row["pwd"], hash_pwd(p, row["salt"])):
            mark_fail(ip)
            return self.j(401, {"msg": "密码错误"})
        status = row.get("status") or "pending"
        if status == "pending":
            return self.j(403, {"code": "PENDING", "msg": "账号已提交，等待管理员审核通过后才能登录"})
        if status == "rejected":
            return self.j(403, {"code": "REJECTED", "msg": "账号未通过审核，请联系管理员（微信：" + WX_NAME + "）"})
        clear_fail(ip)
        turso("UPDATE zb_users SET last_login=? WHERE username=?",
              [(time.strftime("%Y-%m-%d %H:%M"), "text"), (u, "text")])
        # 记录精确到秒的激活时间（"已激活"判定用它，避免同分钟注册的账号被误判为已激活）
        try:
            turso("UPDATE zb_invites SET activated=1 WHERE invitee=? AND activated=0",
                  [(u, "text")])
        except Exception:
            pass
        act = member_active(row)
        mi = member_info(row)
        # "记住我"：签发长期令牌（HttpOnly Cookie，JS 读不到），localStorage 只留用户名
        remember = bool(d.get("remember"))
        pairs = [("zb_sid", make_sid(u), SESSION_HOURS * 3600)]
        if remember:
            try:
                tok, _exp = issue_refresh_token(u)
                pairs.append((REFRESH_COOKIE, tok, REFRESH_DAYS * 86400))
            except Exception:
                pass
        return self.j(200, {"ok": True, "u": u, "member": act, "status": status,
                            "remember": remember,
                            "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"]},
                       {"Set-Cookie": self._cookie_multi(pairs)})

    # ---------- 管理员 ----------
    def api_admin_login(self):
        ip = self.client_ip()
        if too_many(ip):
            return self.j(429, {"msg": "尝试次数过多，请 10 分钟后再试"})
        d = self.body_json()
        user = (d.get("user") or "").strip()
        key = (d.get("key") or "").strip()
        if user != ADMIN_USER or not key or not hmac.compare_digest(key, ADMIN_KEY):
            mark_fail(ip)
            admin_audit("login_fail", user, ip, "wrong key or user")
            return self.j(401, {"msg": "管理员账号或密码错误"})
        clear_fail(ip)
        cookie = "zb_adm=%s; " % admin_sig() + self._cookie_attrs(8 * 3600, admin=True)
        admin_audit("login_ok", ADMIN_USER, ip, "")
        return self.j(200, {"ok": True}, {"Set-Cookie": cookie})

    def _need_admin(self):
        if not self.is_admin():
            self.j(401, {"msg": "管理员未登录"})
            return False
        return True

    def api_admin_key(self):
        """登录后台后查看当前管理员密码。

        用途：管理员密码存在服务器本地的 server_config.json 里，
        手机上打不开该文件，导致忘记密码时无法登录后台。
        这里在**已通过后台鉴权**的前提下回显密码（后台会话 8 小时有效），
        相当于把"看本地配置文件"这件事搬到网页上，手机也能用。
        """
        if not self._need_admin():
            return
        return self.j(200, {"ok": True, "user": ADMIN_USER, "key": ADMIN_KEY})

    def api_admin_reset_key(self):
        """重置管理员密码（已登录后台时可用），生成新的 16 位强随机口令并写回配置文件。"""
        if not self._need_admin():
            return
        global ADMIN_KEY, CONF
        new_key = _strong_admin_key()
        try:
            with open(CONF_PATH, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            conf = dict(CONF)
        conf["admin_key"] = new_key
        try:
            with open(CONF_PATH, "w", encoding="utf-8") as f:
                json.dump(conf, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return self.j(500, {"msg": "写回配置文件失败：" + str(e)})
        CONF = conf
        ADMIN_KEY = new_key
        # 会话签名依赖 ADMIN_KEY，重置后旧后台会话失效 → 需重新登录
        return self.j(200, {"ok": True, "key": new_key,
                            "msg": "已生成新密码，请记好；当前后台会话已失效，请重新登录。"},
                       {"Set-Cookie": "zb_adm=; " + self._cookie_attrs(0, admin=True)})

    # ---------- 后台：云库数据表浏览（只读） ----------
    # 目的：管理员在手机上也能直接查看 Turso 里的用户数据表，
    # 不必再登 Turso 控制台（库挂在别的 group 里，控制台里常常找不到）。
    # 安全：三个接口全部要求后台会话（_need_admin），且 SQL 只读。

    def api_admin_tables(self):
        """列出云库所有表 + 行数 + 字段结构。批量查询，避免逐表往返（Turso 单次往返约 1s）。"""
        if not self._need_admin():
            return
        names = turso("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tabs = [r.get("name") for r in names if r.get("name")]
        if not tabs:
            return self.j(200, {"ok": True, "tables": []})
        stmts = []
        for t in tabs:
            stmts.append(("SELECT COUNT(*) AS c FROM " + _quote_ident(t), None))
            stmts.append(("PRAGMA table_info(" + _quote_ident(t) + ")", None))
        res = turso_multi(stmts)
        out = []
        for i, t in enumerate(tabs):
            cnt_res = res[i * 2] if i * 2 < len(res) else []
            col_res = res[i * 2 + 1] if i * 2 + 1 < len(res) else []
            try:
                cnt = int((cnt_res[0] if cnt_res else {}).get("c") or 0)
            except (TypeError, ValueError):
                cnt = 0
            cols = []
            for c in col_res:
                cn = c.get("name")
                if cn:
                    cols.append({"name": cn, "type": c.get("type") or "", "pk": int(c.get("pk") or 0)})
            out.append({"table": t, "rows": cnt, "cols": cols})
        return self.j(200, {"ok": True, "tables": out})

    def api_admin_rows(self):
        """分页浏览某张表的数据。表名走白名单校验（只允许字母/数字/下划线）。"""
        if not self._need_admin():
            return
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        t = (qs.get("table") or [""])[0]
        if not _safe_ident(t):
            return self.j(400, {"msg": "表名不合法"})
        try:
            lim = min(200, max(1, int((qs.get("limit") or ["50"])[0])))
            off = max(0, int((qs.get("offset") or ["0"])[0]))
        except (TypeError, ValueError):
            lim, off = 50, 0
        q = _quote_ident(t)
        cnt_res, data = turso_multi([
            ("SELECT COUNT(*) AS c FROM " + q, None),
            ("SELECT * FROM " + q + " LIMIT ? OFFSET ?", [(lim, "int"), (off, "int")]),
        ])
        try:
            total = int((cnt_res[0] if cnt_res else {}).get("c") or 0)
        except (TypeError, ValueError):
            total = 0
        return self.j(200, {"ok": True, "table": t, "total": total, "limit": lim, "offset": off,
                            "rows": _mask_secrets(data)})

    def api_admin_sql(self):
        """只读 SQL 查询：仅允许 SELECT / WITH，禁止任何写操作与多语句。"""
        if not self._need_admin():
            return
        d = self.body_json()
        sql = (d.get("sql") or "").strip()
        if not sql:
            return self.j(400, {"msg": "SQL 不能为空"})
        low = sql.lower()
        if not (low.startswith("select") or low.startswith("with")):
            return self.j(400, {"msg": "只允许 SELECT / WITH 查询"})
        # WITH 后面可以跟 INSERT/UPDATE/DELETE，这里再兜一层关键字黑名单。
        # 用词边界匹配，避免 created_at / updated_at / deleted 等普通列名被误伤。
        m = _SQL_WRITE_RE.search(sql)
        if m:
            return self.j(400, {"msg": "检测到写操作关键字「%s」，此处仅允许只读查询" % m.group(1)})
        if ";" in sql.rstrip().rstrip(";"):
            return self.j(400, {"msg": "不支持多语句执行"})
        try:
            rows = turso(sql)
        except Exception as e:
            return self.j(400, {"msg": "查询失败：%s" % e})
        return self.j(200, {"ok": True, "rows": _mask_secrets(rows[:500]), "truncated": len(rows) > 500})

    def serve_admin_tables(self):
        """后台数据表浏览页（静态文件）。鉴权在接口层，页面本身可匿名打开。"""
        fp = os.path.join(SITE_DIR, "admin-tables.html")
        if not os.path.isfile(fp):
            return self.j(404, {"msg": "页面不存在"})
        with open(fp, "rb") as f:
            body = f.read()
        return self._send(200, body, "text/html; charset=utf-8",
                          {"Cache-Control": "no-store"})

    def api_admin_users(self):
        if not self._need_admin():
            return
        rows = turso("SELECT username,status,member,expire,member_tier,member_grade,member_grades,phone,note,created_at,last_login,invited_by "
                     "FROM zb_users ORDER BY created_at DESC")
        dl_set = {g["username"] for g in turso("SELECT username FROM zb_dl")}
        out = []
        for r in rows:
            mi = member_info({"member": r.get("member"), "expire": r.get("expire"),
                              "member_tier": r.get("member_tier"), "member_grade": r.get("member_grade"),
                              "member_grades": r.get("member_grades")})
            out.append({
                "username": r.get("username"),
                "status": r.get("status") or "pending",
                "member": member_active({"member": r.get("member"), "expire": r.get("expire")}),
                "member_tier": mi["tier"], "member_grade": mi["grade"], "member_grades": mi["grades"],
                "expire": r.get("expire") or "",
                "phone": r.get("phone") or "",
                "note": r.get("note") or "",
                "created_at": r.get("created_at") or "",
                "last_login": r.get("last_login") or "",
                "dl": r.get("username") in dl_set,
                "invited_by": r.get("invited_by") or "",
            })
        return self.j(200, {"ok": True, "users": out})

    def api_admin_act(self):
        if not self._need_admin():
            return
        d = self.body_json()
        u = (d.get("u") or "").strip()
        a = (d.get("act") or "").strip()
        admin_audit("admin_act:" + a, u, self.client_ip(), "")
        if not u:
            return self.j(400, {"msg": "缺少用户名"})
        row = get_user(u)
        if not row and a != "del":
            return self.j(404, {"msg": "用户不存在"})
        now = time.strftime("%Y-%m-%d %H:%M")
        year = time.strftime("%Y-%m-%d", time.localtime(time.time() + 365 * 86400))
        if a == "approve":
            turso("UPDATE zb_users SET status='approved', approved_at=? WHERE username=?", [(now, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已通过"})
        if a == "reject":
            turso("UPDATE zb_users SET status='rejected' WHERE username=?", [(u, "text")])
            return self.j(200, {"ok": True, "msg": "已拒绝"})
        if a in ("member", "member_all"):
            turso("UPDATE zb_users SET member=1, status='approved', member_tier='all', member_grade='', member_grades='高一,高二,高三', expire=?, approved_at=? WHERE username=?",
                  [(year, "text"), (now, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已开通全三册版会员至 " + year})
        if a == "member_single":
            g = (d.get("g") or "").strip()
            if g not in ("高一", "高二", "高三"):
                return self.j(400, {"msg": "年级无效，须为 高一/高二/高三"})
            turso("UPDATE zb_users SET member=1, status='approved', member_tier='single', member_grade='', member_grades=?, expire=?, approved_at=? WHERE username=?",
                  [(g, "text"), (year, "text"), (now, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已开通「" + g + "」年级会员至 " + year})
        if a == "member_grade":
            # 按年级开关（单独开通制）：点某个年级 = 「只开通该年级」，
            # 绝不因为历史数据把用户误开成全三册；
            # 若当前「只开着该年级」，再点一次 = 关闭（恢复为仅浏览）。
            g = (d.get("g") or "").strip()
            if g not in ("高一", "高二", "高三"):
                return self.j(400, {"msg": "年级无效，须为 高一/高二/高三"})
            row = get_user(u)
            if not row:
                return self.j(404, {"msg": "用户不存在"})
            grades = parse_grades(row.get("member_grades") or "")
            if grades == [g]:
                # 唯一开着的年级就是它 → 再点一次 = 关闭
                turso("UPDATE zb_users SET member=0, member_tier='', member_grade='', member_grades='' WHERE username=?", [(u, "text")])
                return self.j(200, {"ok": True, "msg": "已关闭「" + g + "」年级会员（恢复为仅浏览）"})
            # 单独开通该年级：覆盖其它年级，绝不自动带上另外两个
            turso("UPDATE zb_users SET member=1, status='approved', member_tier='single', member_grade='', member_grades=?, expire=?, approved_at=? WHERE username=?",
                  [(g, "text"), (year, "text"), (now, "text"), (u, "text")])
            return self.j(200, {"ok": True, "msg": "已单独开通「" + g + "」年级会员至 " + year})
        if a == "unmember":
            turso("UPDATE zb_users SET member=0, member_tier='', member_grade='', member_grades='' WHERE username=?", [(u, "text")])
            return self.j(200, {"ok": True, "msg": "已取消会员"})
        if a == "reset":
            newp = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
            salt = secrets.token_hex(16)
            turso("UPDATE zb_users SET salt=?, pwd=? WHERE username=?",
                  [(salt, "text"), (hash_pwd(newp, salt), "text"), (u, "text")])
            return self.j(200, {"ok": True, "password": newp})
        if a == "del":
            turso("DELETE FROM zb_users WHERE username=?", [(u, "text")])
            return self.j(200, {"ok": True, "msg": "已删除"})
        return self.j(400, {"msg": "未知操作"})

    def log_message(self, fmt, *args):
        # 不打印敏感路径，只记必要信息
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else CONF.get("port") or 8080))
    if not TURSO_TOKEN:
        print("!! server_config.json 里 turso_token 为空，账号功能无法使用")
    db_init()
    migrate_member_grades()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("=" * 56)
    print(" 政史地学习网站 · 服务端已启动")
    print(" 本机访问 : http://127.0.0.1:%d" % port)
    print(" 手机访问 : http://%s:%d   (同一 WiFi)" % (lan_ip(), port))
    print(" 后台审核 : http://127.0.0.1:%d/admin" % port)
    print(" 管理员密码：%s  （已写入 server_config.json 的 admin_key 字段）" % ADMIN_KEY)
    print(" 按 Ctrl+C 停止")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
