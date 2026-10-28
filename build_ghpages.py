# -*- coding: utf-8 -*-
"""构建 GitHub Pages 静态站（gh-pages 分支用）。

设计要点（安全第一）：
  · 只托管「免费版内容」——复用服务端 strip_free_content 规则，剥离 answer / expl / points；
    完整答案、解析、赋分要点、付费下载包一律不进 Pages 分支，付费资产不外泄。
  · 静态站内含 __GH_STATIC__ 标志：启动先探活云端，可用则弹出「切换到云端版」提示（由用户手动确认，
    不自动跳走），不可用则读同目录 content.json 进离线浏览（托底），资料浏览与做题照常可用。

用法：
    python build_ghpages.py              # 只生成 gh-pages/ 目录
    python build_ghpages.py --publish    # 生成 + 通过 Git Data API 发布到 gh-pages 分支 + 触发 Pages 重建
    python build_ghpages.py --push       # 生成 + git push 推送（网络不稳定时改用 --publish）
"""
import json, os, shutil, subprocess, sys, argparse, gzip, time

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
    ap.add_argument("--push", action="store_true", help="构建后用 git push 推送 gh-pages 分支")
    ap.add_argument("--publish", action="store_true", help="构建后用 Git Data API 发布 gh-pages 并触发 Pages 重建")
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

    if a.push or a.publish:
        if a.publish:
            publish_via_api(OUT)
        else:
            print("\n提交并推送到 gh-pages 分支…")
            cmds = [
                ["git", "-C", BASE, "checkout", "-B", GH_PAGES_BRANCH],
                ["git", "-C", BASE, "add", "-f", "gh-pages"],
                ["git", "-C", BASE, "commit", "-q", "-m",
                 "build: GitHub Pages 静态站（免费版内容：题干/卡片/导图，无答案解析与付费下载包）\n\n"
                 "- 云端可用时弹出「切换到云端版」提示（用户手动确认，不自动跳走），也可留在本页\n"
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


def publish_via_api(outdir):
    """用 Git Data API 发布 gh-pages 分支（比 git push 稳：逐个小请求，避开大包连接问题）。
    同时触发 Pages 重建。"""
    import base64
    REPO = "repos/ku521/52ZX"

    def api(path, method="GET", data=None):
        p = path.lstrip("/")
        if not p.startswith("repos/"):
            p = REPO + "/" + p
        cmd = ["gh", "api", p, "-X", method]
        if data is not None:
            cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=(json.dumps(data) if data is not None else None),
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise SystemExit("API 失败 %s %s → %s" % (method, p, (r.stderr or r.stdout)[:300]))
        out = r.stdout
        return json.loads(out) if out.strip() else {}

    print("\n通过 Git Data API 发布 gh-pages 分支…")
    files = []
    for root, _, fs in os.walk(outdir):
        for f in fs:
            p = os.path.join(root, f)
            files.append((os.path.relpath(p, outdir).replace("\\", "/"), p))
    files.sort()
    entries = []
    for rel, p in files:
        b = open(p, "rb").read()
        blob = api("git/blobs", "POST", {"content": base64.b64encode(b).decode(), "encoding": "base64"})
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    entries.append({"path": ".nojekyll", "mode": "100644", "type": "blob",
                     "sha": api("git/blobs", "POST",
                                {"content": base64.b64encode(b"probe").decode(),
                                 "encoding": "base64"})["sha"]})
    print("  上传 blob %d 个" % len(entries))
    tree = api("git/trees", "POST", {"base_tree": None, "tree": entries})
    main_sha = api("git/ref/heads/main")["object"]["sha"]
    commit = api("git/commits", "POST", {
        "message": "build: GitHub Pages 静态站（免费版内容）\n\n"
                   "- 云端可用时弹出「切换到云端版」提示（用户手动确认，不自动跳走），也可留在本页\n"
                   "- 云端不可用时读同目录 content.json 进离线浏览，作为托底\n"
                   "- ?gh=offline 可强制离线版\n"
                   "- 内容已剥离 answer/expl/points，付费下载包不随站发布",
        "tree": tree["sha"], "parents": [main_sha]})
    print("  commit %s" % commit["sha"][:10])
    try:
        api("git/refs", "POST", {"ref": "refs/heads/gh-pages", "sha": commit["sha"]})
        print("  分支 gh-pages 已创建")
    except SystemExit as e:
        if "already exists" in str(e):
            api("git/refs/heads/gh-pages", "PATCH", {"sha": commit["sha"], "force": True})
            print("  分支 gh-pages 已更新(force)")
        else:
            raise
    # 切 Pages 源 + 触发重建（切源不会自动重建，必须显式触发）
    try:
        api("pages", "PUT", {"source": {"branch": GH_PAGES_BRANCH, "path": "/"}})
        print("  Pages 源已切到 gh-pages")
    except SystemExit:
        print("  (Pages 源设置跳过，可能已是 gh-pages)")
    api("pages/builds", "POST", {})
    print("  已触发 Pages 重建，等待完成…")
    for i in range(24):
        time.sleep(10)
        b = api("pages/builds/latest")
        st, cm = b.get("status"), (b.get("commit") or "")[:8]
        print("    [%3ds] %s %s" % ((i + 1) * 10, st, cm))
        if st == "built":
            break
    print("  ✓ gh-pages 已发布并构建完成")


if __name__ == "__main__":
    main()
