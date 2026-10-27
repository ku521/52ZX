# -*- coding: utf-8 -*-
"""构建 GitHub Pages 静态站（gh-pages 分支用）。

设计要点（安全第一）：
  · 只托管「免费版内容」——复用服务端 strip_free_content 规则，剥离 answer / expl / points；
    完整答案、解析、赋分要点、付费下载包一律不进 Pages 分支，付费资产不外泄。
  · 静态站内含 __GH_STATIC__ 标志：启动先探活云端，可用则 1.6s 自动跳转（速度优先），
    不可用则读同目录 content.json 进离线浏览（托底），资料浏览与做题照常可用。

用法：
    python build_ghpages.py            # 生成 gh-pages/ 目录
    python build_ghpages.py --push     # 生成后提交并推送到 gh-pages 分支
"""
import json, os, shutil, subprocess, sys, argparse, gzip

BASE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(BASE, "site")
OUT = os.path.join(BASE, "gh-pages")
CLOUD = "https://52zx.app.workbuddy.host/?sharecode=E2G3MZIBK2"
GH_PAGES_BRANCH = "gh-pages"

# 静态站要带的资源（相对 site/）
ASSET_FILES = [
    "index.html", "manifest.webmanifest", "sw.js",
    "apple-touch-icon.png", "favicon-32.png", "icon-192.png", "icon-512.png",
    "logo-brand.png", "logo-transparent.png",
]
ASSET_DIRS = ["img"]

# 剥离字段（与服务端 strip_free_content 保持一致）
DROP_Q = ("answer", "expl")
DROP_E = ("points",)


def strip_free(data):
    """免费版内容：去答案/解析/赋分要点；cards、roadmap、memory 等浏览内容保留。"""
    d = dict(data)
    if isinstance(d.get("questions"), list):
        d["questions"] = [{k: v for k, v in q.items() if k not in DROP_Q} for q in d["questions"]]
    if isinstance(d.get("essays"), list):
        d["essays"] = [{k: v for k, v in e.items() if k not in DROP_E} for e in d["essays"]]
    if isinstance(d.get("papers"), list):
        papers = []
        for p in d["papers"]:
            if isinstance(p, dict) and isinstance(p.get("items"), list):
                np = dict(p)
                np["items"] = [{k: v for k, v in it.items() if k not in DROP_Q} for it in p["items"]]
                papers.append(np)
            else:
                papers.append(p)
        d["papers"] = papers
    return d


def build_index(src, dst):
    """注入静态模式标志：__GH_STATIC__ / __GH_CLOUD__"""
    s = open(src, encoding="utf-8").read()
    inject = ("<script>window.__GH_STATIC__=true;"
              "window.__GH_CLOUD__=%s;</script>\n" % json.dumps(CLOUD))
    # 插到第一个 <script> 之前，保证主脚本执行前标志已就位
    i = s.find("<script")
    if i < 0:
        raise SystemExit("index.html 里找不到 <script> 标签")
    s = s[:i] + inject + s[i:]
    open(dst, "w", encoding="utf-8").write(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="构建后提交并推送到 gh-pages 分支")
    a = ap.parse_args()

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # 1) 静态资源
    n_asset = 0
    for f in ASSET_FILES:
        p = os.path.join(SITE, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(OUT, f))
            n_asset += 1
        else:
            print("  ! 缺少资源:", f)
    for d in ASSET_DIRS:
        p = os.path.join(SITE, d)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(OUT, d))
            n_asset += 1

    # 2) 注入静态标志的 index.html
    build_index(os.path.join(SITE, "index.html"), os.path.join(OUT, "index.html"))

    # 3) 免费版 content.json
    src = os.path.join(SITE, "content.json")
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    free = strip_free(data)
    with open(os.path.join(OUT, "content.json"), "w", encoding="utf-8") as f:
        json.dump(free, f, ensure_ascii=False, separators=(",", ":"))

    # 4) 校验：确保没有答案残留
    leak = []
    q0 = (free.get("questions") or [{}])[0]
    if "answer" in q0 or "expl" in q0:
        leak.append("questions 仍含 answer/expl")
    e0 = (free.get("essays") or [{}])[0]
    if "points" in e0:
        leak.append("essays 仍含 points")
    p0 = ((free.get("papers") or [{}])[0] or {}).get("items") or [{}]
    if p0 and ("answer" in p0[0] or "expl" in p0[0]):
        leak.append("papers 仍含 answer/expl")
    if os.path.isdir(os.path.join(OUT, "files")):
        leak.append("files/ 付费下载包被复制进来了")
    if leak:
        raise SystemExit("构建中止（检测到付费内容泄漏）：\n  - " + "\n  - ".join(leak))

    # 5) 统计
    raw = os.path.getsize(os.path.join(OUT, "content.json"))
    gz = len(gzip.compress(open(os.path.join(OUT, "content.json"), "rb").read(), 6))
    total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(OUT) for f in fs)
    print("构建完成 → gh-pages/")
    print("  资源文件     : %d 个" % n_asset)
    print("  content.json : %.2f MB（gzip %.2f MB）｜题 %d / 卡 %d / 大题 %d / 卷 %d"
          % (raw / 1048576, gz / 1048576, len(free.get("questions") or []),
             len(free.get("cards") or []), len(free.get("essays") or []),
             len(free.get("papers") or [])))
    print("  站点总大小   : %.2f MB" % (total / 1048576))
    print("  安全校验     : 通过（无 answer/expl/points，无付费下载包）")

    if a.push:
        print("\n提交并推送到 gh-pages 分支…")
        cmds = [
            ["git", "-C", BASE, "checkout", "-B", GH_PAGES_BRANCH],
            ["git", "-C", BASE, "add", "-f", "gh-pages"],
            ["git", "-C", BASE, "commit", "-q", "-m",
             "build: GitHub Pages 静态站（免费版内容：题干/卡片/导图，无答案解析与付费下载包）\n\n"
             "- 云端可用时 1.6s 自动跳转云端（速度优先），可选择留在本页\n"
             "- 云端不可用时读同目录 content.json 进离线浏览，作为托底\n"
             "- 内容已剥离 answer/expl/points，付费资产不外泄"],
            ["git", "-C", BASE, "push", "-f", "origin", GH_PAGES_BRANCH],
            ["git", "-C", BASE, "checkout", "main"],
        ]
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode != 0:
                print("  ✗", " ".join(c[3:]), "→", (r.stderr or r.stdout).strip()[:200])
                if "checkout" in c[1] and "gh-pages" in c:
                    subprocess.run(["git", "-C", BASE, "checkout", "main"], capture_output=True)
                raise SystemExit(1)
            out = (r.stdout or r.stderr).strip()
            if out:
                print("  ·", out.splitlines()[0][:120])
        print("  ✓ 已推送 gh-pages 分支")


if __name__ == "__main__":
    main()
