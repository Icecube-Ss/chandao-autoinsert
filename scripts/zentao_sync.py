# -*- coding: utf-8 -*-
"""禅道缺陷同步 · 报告缺陷 → 禅道 Bug

安全默认：plan 只读演练；apply 才写入，且写入后自动回读校验。
用法见 SKILL.md，契约细节见 references/api-contract.md。
"""
import argparse
import datetime
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = {
    "base_url": "",
    "product_id": 0,
    "assignee": "",
    "opened_build": "1",
    "default_type": "codeerror",
    "severity_map": {"P0": 1, "P1": 2, "P2": 3, "P3": 4, "P4": 4},
    "pri_map": {"P0": 1, "P1": 2, "P2": 3, "P3": 4, "P4": 4},
    "agent_short": "",
    "sync_log": ".push/sync.jsonl",
}


# ────────────────────────── 跨环境兼容 ──────────────────────────

def setup_console():
    """Windows 控制台默认 GBK(936)，中文输出会变成乱码。

    这是「同一脚本 A 机器正常、B 机器输出乱码」最常见的原因，必须在任何输出前修好。
    三级兜底：① 改代码页 ② reconfigure ③ 重新包一层 TextIOWrapper。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # Python 3.7+
            continue
        except Exception:
            pass
        try:
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass   # 实在不行就算了，重要输出正文是 ASCII/JSON 仍可读


setup_console()


# ────────────────────────── 配置与凭据 ──────────────────────────

def load_json(path, default=None):
    """utf-8-sig：Windows 记事本保存的 JSON 带 BOM，用纯 utf-8 读会报 Expecting value"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with io.open(path, encoding=enc) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
        except Exception:
            return default
    return default


def first_file(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def resolve_config(cli_path):
    """配置查找顺序：CLI → 环境变量 → 当前目录 → 技能目录 → 内置默认"""
    p = cli_path or os.environ.get("ZENTAO_SYNC_CONFIG")
    p = first_file(p, os.path.join(os.getcwd(), "zentao_sync.config.json"),
                   os.path.join(SKILL_DIR, "config.json"))
    cfg = dict(DEFAULT_CONFIG)
    if p:
        cfg.update(load_json(p, {}) or {})
        cfg["_from"] = p
    return cfg


MISSING_SECRET = ("缺少禅道凭据：请向用户索取登录授权后再执行，不要擅自复用历史凭据文件。\n"
                  "三种给法任选：① 用户提供账号口令后写入 secret.local.json（{\"account\":\"...\",\"password\":\"...\"}）；"
                  "② 环境变量 ZENTAO_ACCOUNT / ZENTAO_PASSWORD；③ --secret 指向已有文件。\n"
                  "凭据只放本地，禁止入库、禁止进报告、禁止上传云端。")


def resolve_secret(cli_path, required=True):
    """凭据查找顺序：CLI → 环境变量文件 → 当前目录 → 技能目录 → 环境变量直给

    required=False 时不退出，返回 (None, None)，供 auth 子命令报告「缺凭据」。
    """
    p = cli_path or os.environ.get("ZENTAO_SYNC_SECRET")
    p = first_file(p, os.path.join(os.getcwd(), "secret.local.json"),
                   os.path.join(SKILL_DIR, "secret.local.json"))
    sec = load_json(p, {}) or {}
    src = p
    if not sec.get("account"):
        sec["account"] = os.environ.get("ZENTAO_ACCOUNT", "")
        sec["password"] = os.environ.get("ZENTAO_PASSWORD", "")
        if sec["account"]:
            src = "env:ZENTAO_ACCOUNT"
    if not sec.get("account") or not sec.get("password"):
        if required:
            sys.exit(MISSING_SECRET)
        return None, None
    return sec, src


# ────────────────────────── HTTP ──────────────────────────

INSECURE = os.environ.get("ZENTAO_SYNC_INSECURE", "").strip().lower() in ("1", "true", "yes", "on")
_SSL_CTX = [None]


def ssl_context():
    """禅道常部署自签证书，https 下默认校验必然失败。

    ZENTAO_SYNC_INSECURE=1 时才跳过校验——默认仍严格校验，不静默弱化安全。
    """
    if _SSL_CTX[0] is None:
        import ssl
        _SSL_CTX[0] = ssl._create_unverified_context() if INSECURE else ssl.create_default_context()
    return _SSL_CTX[0]


def http_json(url, method="GET", payload=None, token=None, timeout=25):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Token"] = token
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    kw = {}
    if url.lower().startswith("https"):
        kw["context"] = ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, **kw) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body[:400]
    except Exception as e:
        hint = ""
        if "SSL" in type(e).__name__ or "CERTIFICATE" in str(e).upper():
            hint = "（自签证书？设 ZENTAO_SYNC_INSECURE=1 可跳过校验，仅限内网自建）"
        return -1, (repr(e)[:180] + hint)


class Zentao(object):
    def __init__(self, base, account, password):
        self.base = base.rstrip("/")
        self.account = account
        self.password = password
        self.token = None

    def login(self):
        st, r = http_json(self.base + "/api.php/v1/tokens", "POST",
                          {"account": self.account, "password": self.password})
        if st not in (200, 201) or not isinstance(r, dict) or not r.get("token"):
            raise SystemExit("禅道登录失败：HTTP %s %s" % (st, str(r)[:200]))
        self.token = r["token"]
        return self.token

    def get(self, path):
        if not self.token:
            self.login()
        return http_json(self.base + path, token=self.token)

    def bugs(self, product_id, limit=1000):
        st, r = self.get("/api.php/v1/products/%s/bugs?limit=%d" % (product_id, limit))
        if st != 200:
            raise SystemExit("拉取缺陷列表失败：HTTP %s %s" % (st, str(r)[:200]))
        return r.get("bugs", [])

    def bug(self, bug_id):
        st, r = self.get("/api.php/v1/bugs/%s" % bug_id)
        return (r if st == 200 and isinstance(r, dict) else None)

    def create(self, product_id, payload):
        return http_json("%s/api.php/v1/products/%s/bugs" % (self.base, product_id),
                         "POST", payload, token=self.token)

    def patch(self, bug_id, payload):
        return http_json("%s/api.php/v1/bugs/%s" % (self.base, bug_id),
                         "PUT", payload, token=self.token)

    def users(self, limit=200):
        """/users 实测可用，返回账号 + realname，是「指派给谁」唯一可信的枚举来源"""
        st, r = self.get("/api.php/v1/users?limit=%d" % limit)
        if st != 200 or not isinstance(r, dict):
            return [], "HTTP %s" % st
        return r.get("users", []), ""

    def products(self, limit=200):
        st, r = self.get("/api.php/v1/products?limit=%d" % limit)
        if st != 200 or not isinstance(r, dict):
            return [], "HTTP %s" % st
        return r.get("products", []), ""


# ────────────────────────── 文本构造 ──────────────────────────

def esc(s):
    return html.escape(str(s or ""), quote=False)


def normalize(title):
    """归一化标题用于跨来源比对：剥离格式前缀与任意位置的 [SRC:xxx] 幂等键。

    同时兼容三代标题：
      旧版  ``[SRC:x] 问题描述``
      新版  ``[AGENT-Px-X]-问题描述 [SRC:x]``
      纯文本 ``问题描述``
    幂等键改到标题末尾后，旧实现会把它之后的内容全部截掉（返回空串），
    导致 by_bare 索引失效；这里改为「剥离标记」而非「截断到标记之后」。
    """
    t = title or ""
    t = re.sub(r"\[SRC:[^\]]*\]", "", t)
    t = re.sub(r"^\s*\[[A-Za-z][A-Za-z0-9]*-P\d-\d+\]-\s*", "", t)
    return " ".join(t.replace("\u3000", " ").split()).lower()


def steps_html(d, report_url=""):
    """缺陷四要素 → 禅道 steps（HTML）。报告链接先转义再包标签，否则会二次转义成乱码。"""
    parts = ["<p>[SRC:%s] %s · %s</p>" % (esc(d.get("id")), esc(d.get("lv")), esc(d.get("ep") or ""))]
    if report_url:
        u = esc(report_url)
        parts.append('<p><b>来源报告</b>：<a href="%s" target="_blank">%s</a></p>' % (u, u))
    elif d.get("report"):
        parts.append("<p><b>来源报告</b>：%s</p>" % esc(d.get("report")))
    for label, val in (
        ("<b>缺陷编号</b>", d.get("id")),
        ("<b>等级</b>", d.get("lv")),
        ("<b>用例</b>", d.get("case")),
        ("<b>复现步骤 / 证据</b>", d.get("ev")),
        ("<b>实际结果</b>", d.get("actual") or "见上"),
        ("<b>期望结果</b>", d.get("expect") or "按契约应返回业务成功或明确错误码，不得静默成功或返回未定义错误码"),
        ("<b>影响</b>", d.get("fx")),
        ("<b>修复建议</b>", d.get("sug")),
    ):
        if val:
            parts.append("<p>%s：%s</p>" % (label, esc(val)))
    return "".join(parts)


def signature(d):
    """幂等键 [SRC:<缺陷号>]：固定置于标题末尾，steps 首行也保留同一份。"""
    return "[SRC:%s]" % d["id"]


TITLE_LV = {"P0": "P0", "P1": "P1", "P2": "P2", "P3": "P3", "P4": "P3",
            "1": "P0", "2": "P1", "3": "P2", "4": "P3"}


def title_text(d, cfg):
    """统一标题格式：[<Agent缩写>-Px-X]-问题描述 [SRC:<缺陷号>]

    Px = 严重程度（severity 1/2/3/4 → P0/P1/P2/P3）；X = 优先级 pri 原值（不带 P）。
    agent_short 由配置给出（例如 ABC），缺省回退 AGENT，不写完整署名。
    幂等键在末尾：existing_index.by_src 位置无关，verify_bug 用本函数回比。
    """
    lv = str(d.get("lv", "P2")).upper()
    px = TITLE_LV.get(lv, "P2")
    x = cfg.get("pri_map", {}).get(lv, 3)
    short = str(cfg.get("agent_short") or d.get("agent_short") or "AGENT").strip()
    return ("[%s-%s-%s]-%s %s" % (short, px, x, d.get("title", ""), signature(d)))[:200]


def build_payload(d, cfg, report_url=""):
    lv = d.get("lv", "P2")
    return {
        "title": title_text(d, cfg),
        "severity": cfg["severity_map"].get(lv, 3),
        "pri": cfg["pri_map"].get(lv, 3),
        "type": cfg.get("default_type", "codeerror"),
        "module": 0,
        "execution": 0,
        "openedBuild": str(cfg.get("opened_build", "1")),
        "assignedTo": cfg.get("assignee", ""),
        "steps": steps_html(d, report_url),
    }


def existing_index(bugs):
    by_title, by_bare, by_src = {}, {}, {}
    for b in bugs:
        t = (b.get("title") or "").strip()
        by_title[t] = b
        by_bare[normalize(t)] = b
        if "[SRC:" in t and "]" in t.split("[SRC:")[1]:
            by_src[t.split("[SRC:")[1].split("]")[0]] = b
    return by_title, by_bare, by_src


# ────────────────────────── 校验 ──────────────────────────

def verify_bug(d, snap, cfg):
    """回读校验：字段是否按预期落库。返回问题列表（空＝通过）"""
    bad = []
    if not snap:
        return ["读不回来（GET /bugs/{id} 失败）"]
    if (snap.get("title") or "") != build_payload(d, cfg)["title"]:
        bad.append("标题不一致")
    if snap.get("severity") != cfg["severity_map"].get(d.get("lv", "P2"), 3):
        bad.append("severity=%s 期望 %s" % (snap.get("severity"), cfg["severity_map"].get(d.get("lv", "P2"))))
    if snap.get("pri") != cfg["pri_map"].get(d.get("lv", "P2"), 3):
        bad.append("pri=%s 期望 %s" % (snap.get("pri"), cfg["pri_map"].get(d.get("lv", "P2"))))
    who = (snap.get("assignedTo") or {}).get("account")
    if cfg.get("assignee") and who != cfg["assignee"]:
        bad.append("指派=%s 期望 %s" % (who, cfg["assignee"]))
    build = (snap.get("openedBuild") or [{}])[0].get("id")
    if build != str(cfg.get("opened_build", "1")):
        bad.append("构建=%s 期望 %s" % (build, cfg.get("opened_build")))
    if "&lt;a href" in (snap.get("steps") or ""):
        bad.append("steps 存在二次转义（&lt;a href）")
    if len(snap.get("steps") or "") < 80:
        bad.append("steps 过短（%d 字符），可能有内容丢失" % len(snap.get("steps") or ""))
    return bad


# ────────────────────────── 主流程 ──────────────────────────

def load_defects(path):
    data = load_json(path)
    if data is None:
        sys.exit("读不到缺陷文件：%s" % path)
    if isinstance(data, list):
        return data, ""
    return data.get("defects", []), data.get("report_url", "")


# ────────────────────────── 字段枚举检索（交互确认前置） ──────────────────────────

def dist_of(bugs, key):
    c = {}
    for b in bugs:
        k = b.get(key)
        c[str(k)] = c.get(str(k), 0) + 1
    return [{"value": k, "count": v} for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))]


def build_titles(b):
    """openedBuild 两种形态：列表接口是逗号串「构建A,构建B」，详情接口是 [{"id":"1","title":...}]"""
    v = b.get("openedBuild")
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [(x.get("title") if isinstance(x, dict) else str(x)) for x in v if x]
    return []


def infer_builds(bugs):
    """该实例无 /builds 接口时，构建枚举只能从现有缺陷反推——有量才有说服力，used_by_bugs 就是证据"""
    seen = {}
    for b in bugs:
        for n in build_titles(b):
            seen[n] = seen.get(n, 0) + 1
    return [{"title": k, "used_by_bugs": v} for k, v in sorted(seen.items(), key=lambda kv: -kv[1])]


def probe_caps(zt, pid, bug_id=None):
    """哪些枚举接口真的存在。404 的项必须在交互时如实告知用户「无此枚举」，不能编造选项"""
    caps = {}
    for name, path in (("builds", "/api.php/v1/products/%s/builds" % pid),
                       ("modules", "/api.php/v1/products/%s/modules" % pid),
                       ("executions", "/api.php/v1/products/%s/executions" % pid)):
        st, _ = zt.get(path)
        caps[name] = st
    if bug_id:
        st, _ = zt.get("/api.php/v1/bugs/%s/comments" % bug_id)
        caps["comments"] = st
    return caps


def collect_meta(zt, cfg, sec_src, account):
    """一次拿全「交互确认」需要的枚举：产品 / 指派账号 / 构建 / 严重程度 / 优先级 / 类型"""
    pid = cfg["product_id"]
    bugs = zt.bugs(pid)
    users, uerr = zt.users()
    prods, perr = zt.products()
    caps = probe_caps(zt, pid, bugs[0].get("id") if bugs else None)
    cur = [p for p in prods if str(p.get("id")) == str(pid)]
    assignees = {}
    for b in bugs:
        a = b.get("assignedTo")
        a = a.get("account") if isinstance(a, dict) else a
        if a:
            assignees[a] = assignees.get(a, 0) + 1
    warnings = []
    if caps.get("builds") == 404:
        warnings.append("无 /products/{id}/builds 接口，构建枚举由现有缺陷反推（共 %d 个）" % len(infer_builds(bugs)))
    if caps.get("modules") == 404:
        warnings.append("无 /products/{id}/modules 接口，module 只能写 0，缺陷无法按模块归类（可用标题前缀区分）")
    if caps.get("executions") == 404:
        warnings.append("无 /products/{id}/executions 接口，execution 只能写 0")
    if uerr:
        warnings.append("拉取用户列表失败（%s），指派账号需用户手工提供" % uerr)
    if perr:
        warnings.append("拉取产品列表失败（%s）" % perr)
    return {
        "base_url": cfg["base_url"],
        "account": account,
        "credential_source": sec_src,
        "product_id": pid,
        "product_name": (cur[0].get("name") if cur else ""),
        "bug_count": len(bugs),
        "products": [{"id": p.get("id"), "name": p.get("name"), "status": p.get("status")} for p in prods],
        "users": [{"account": u.get("account"), "realname": u.get("realname")} for u in users],
        "assignee_history": [{"account": k, "count": v} for k, v in sorted(assignees.items(), key=lambda kv: -kv[1])],
        "builds": infer_builds(bugs),
        "enums": {
            "severity": dist_of(bugs, "severity"),
            "pri": dist_of(bugs, "pri"),
            "type": dist_of(bugs, "type"),
            "status": dist_of(bugs, "status"),
        },
        "current_config": {"assignee": cfg.get("assignee"), "opened_build": cfg.get("opened_build"),
                           "default_type": cfg.get("default_type"),
                           "severity_map": cfg.get("severity_map"), "pri_map": cfg.get("pri_map")},
        "capabilities": caps,
        "warnings": warnings,
    }


def do_auth(cfg, sec, sec_src):
    """登录授权核验：凭据来源、账号身份、产品可读性、写权限间接证据（该产品下本账号已提单数）"""
    out = {"ok": False, "base_url": cfg.get("base_url"), "account": sec.get("account"),
           "credential_source": sec_src, "product_id": cfg.get("product_id")}
    zt = Zentao(cfg["base_url"], sec["account"], sec["password"])
    try:
        zt.login()
    except SystemExit as e:
        out["error"] = str(e)
        out["hint"] = ("登录请求未完成，通常是 base_url 配错或网络不通。当前 base_url=%s，"
                       "请确认它指向你能访问的禅道，或改 config 里的地址。" % cfg.get("base_url"))
        return out, zt
    out["login"] = "ok"
    users, uerr = zt.users()
    prods, perr = zt.products()
    out["users_visible"] = len(users)
    out["products_visible"] = len(prods)
    if uerr or perr:
        out["error"] = "；".join(x for x in (uerr, perr) if x)
    pid = cfg.get("product_id")
    bugs = []
    if pid:
        try:
            bugs = zt.bugs(pid)
            out["can_read_product"] = True
        except SystemExit as e:
            out["can_read_product"] = False
            out["error"] = str(e)
    cur = [p for p in prods if str(p.get("id")) == str(pid)]
    out["product_name"] = cur[0].get("name") if cur else ""
    out["bug_count"] = len(bugs)
    out["written_by_account"] = len([
        b for b in bugs
        if (b.get("openedBy").get("account") if isinstance(b.get("openedBy"), dict) else b.get("openedBy")) == sec["account"]])
    out["ok"] = bool(out.get("can_read_product"))
    return out, zt


def print_meta(m):
    print("禅道 %s · 账号 %s（凭据来源：%s）" % (m["base_url"], m["account"], m["credential_source"]))
    print("当前产品：%s %s（现有缺陷 %d 条）" % (m["product_id"], m["product_name"], m["bug_count"]))
    print("\n[产品] " + " / ".join("%s:%s" % (p["id"], p["name"]) for p in m["products"]))
    print("[指派账号] " + " / ".join(u["account"] for u in m["users"]))
    if m["assignee_history"]:
        print("[历史接单] " + " / ".join("%s(%d)" % (a["account"], a["count"]) for a in m["assignee_history"]))
    print("[构建] " + (" / ".join("%s(%d)" % (b["title"], b["used_by_bugs"]) for b in m["builds"]) or "无"))
    for k in ("severity", "pri", "type", "status"):
        print("[%s] " % k + " / ".join("%s×%d" % (e["value"], e["count"]) for e in m["enums"][k]))
    if m["warnings"]:
        print("\n注意：")
        for w in m["warnings"]:
            print("  - " + w)


def print_auth(a):
    if a.get("error"):
        print("✗ %s" % a["error"])
    if a.get("hint"):
        print("  提示：%s" % a["hint"])
    if not a.get("login"):
        print("登录未通过：账号 %s @ %s（凭据来源 %s）" % (a.get("account"), a.get("base_url"), a.get("credential_source")))
        return
    print("✓ 登录成功：%s @ %s（凭据来源：%s）" % (a.get("account"), a.get("base_url"), a.get("credential_source")))
    print("  可见用户 %s / 可见产品 %s" % (a.get("users_visible"), a.get("products_visible")))
    print("  目标产品：%s %s，缺陷 %s 条" % (a.get("product_id"), a.get("product_name"), a.get("bug_count")))
    print("  该产品下本账号已提单：%s 条（>0 即写权限已被历史证明）" % a.get("written_by_account"))


def add_common(p, suppress=False):
    """全局参数同时挂到主命令与各子命令，使 --secret 写在子命令前后都能生效"""
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    p.add_argument("--config", help="配置文件路径", **kw)
    p.add_argument("--secret", help="凭据文件路径", **kw)
    p.add_argument("--product", type=int, help="覆盖产品 ID", **kw)
    p.add_argument("--assignee", help="覆盖指派人账号", **kw)
    return p


def main():
    ap = argparse.ArgumentParser(description="禅道缺陷同步：报告缺陷 → 禅道 Bug")
    add_common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("auth", "登录授权核验：凭据来源 + 身份 + 产品可读/写证据"),
                            ("meta", "字段枚举检索：产品 / 指派账号 / 构建 / severity / pri / type"),
                            ("list", "列出现有缺陷（查重基线 / 看现状）"),
                            ("plan", "只读演练：打印将要提交的 payload"),
                            ("apply", "真实写入（create），写后自动回读校验"),
                            ("verify", "按缺陷文件回读校验已建单字段"),
                            ("patch-steps", "按最新渲染逻辑重写指定单的 steps")):
        sp = sub.add_parser(name, help=help_text)
        add_common(sp, suppress=True)
        if name in ("plan", "apply", "verify", "patch-steps"):
            sp.add_argument("--defects", required=True, help="缺陷 JSON 文件")
        if name in ("auth", "meta"):
            sp.add_argument("--json", action="store_true", help="输出 JSON（供交互层构造选项）")
        if name == "apply":
            sp.add_argument("--limit", type=int, default=0, help="本次最多创建几条（首跑建议 1）")
            sp.add_argument("--force", action="store_true", help="忽略查重强建（慎用）")
            sp.add_argument("--dry-run", action="store_true", help="等价于 plan")
        if name == "patch-steps":
            sp.add_argument("--bug", required=True, help="要修的单号，如 48")

    args = ap.parse_args()
    cfg = resolve_config(getattr(args, "config", None))
    if getattr(args, "product", None):
        cfg["product_id"] = args.product
    if getattr(args, "assignee", None):
        cfg["assignee"] = args.assignee

    # base_url 是所有子命令的硬前提；product_id 只有查/写缺陷时才需要
    if not cfg.get("base_url"):
        sys.exit("配置缺 base_url。当前读到的是示例配置（%s），里面的地址是占位值。\n"
                 "请在当前工作目录新建 zentao_sync.config.json 并填写 base_url"
                 "（结尾不带 /api.php，如 http://zentao.example.com），或用 --config 指定文件。\n"
                 "配好后依次跑：auth 核验授权 → meta 检索字段枚举 → 与用户确认。"
                 % cfg.get("_from", "内置默认"))

    # ── auth：无凭据也要能跑，用来告诉调用方「该向用户索取授权了」──
    if args.cmd == "auth":
        sec, src = resolve_secret(getattr(args, "secret", None), required=False)
        if not sec:
            if args.json:
                print(json.dumps({"ok": False, "error": "missing_credential", "hint": MISSING_SECRET},
                                 ensure_ascii=False, indent=2))
            else:
                print(MISSING_SECRET)
            return 2
        out, _ = do_auth(cfg, sec, src)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print_auth(out)
        return 0 if out.get("ok") else 2

    if not cfg.get("product_id"):
        sys.exit("配置缺 product_id：用 --config 指定，或先跑 meta 看产品列表后写入配置")

    sec, sec_src = resolve_secret(getattr(args, "secret", None))
    zt = Zentao(cfg["base_url"], sec["account"], sec["password"])
    zt.login()

    # ── meta：交互确认前必须先跑，选项只能来自真实枚举 ──
    if args.cmd == "meta":
        m = collect_meta(zt, cfg, sec_src, sec["account"])
        if args.json:
            print(json.dumps(m, ensure_ascii=False, indent=2))
        else:
            print_meta(m)
        return 0

    # ── list ──
    if args.cmd == "list":
        bugs = zt.bugs(cfg["product_id"])
        print("产品 %s 共 %d 条缺陷" % (cfg["product_id"], len(bugs)))
        for b in sorted(bugs, key=lambda x: x.get("id", 0)):
            print("%5s | %-8s | sev%s | %s" % (b.get("id"), b.get("status"), b.get("severity"),
                                               (b.get("title") or "")[:88]))
        return 0

    defects, report_url = load_defects(args.defects)
    if not defects:
        sys.exit("缺陷文件里没有 defects 条目：%s" % args.defects)

    # ── patch-steps ──
    if args.cmd == "patch-steps":
        hit = None
        for d in defects:
            st, r = zt.get("/api.php/v1/bugs/%s" % args.bug)
            snap = r if st == 200 and isinstance(r, dict) else None
            if snap and ("[SRC:%s]" % d["id"]) in (snap.get("title") or ""):
                hit = d
                break
        if not hit:
            sys.exit("单 #%s 的标题里找不到匹配的 [SRC:xxx] 标记，无法确定对应哪条缺陷" % args.bug)
        st, r = zt.patch(args.bug, {"steps": steps_html(hit, report_url)})
        print("patch #%s ← %s : HTTP %s" % (args.bug, hit["id"], st))
        bad = verify_bug(hit, zt.bug(args.bug), cfg)
        print("校验：" + ("通过" if not bad else "；".join(bad)))
        return 0 if st == 200 and not bad else 1

    # ── verify ──
    if args.cmd == "verify":
        bugs = zt.bugs(cfg["product_id"])
        _, _, by_src = existing_index(bugs)
        ok = fail = missing = 0
        for d in defects:
            b = by_src.get(d["id"])
            if not b:
                print("缺失 %-10s 禅道中无 [SRC:%s] 的单" % (d["id"], d["id"]))
                missing += 1
                continue
            bad = verify_bug(d, zt.bug(b["id"]), cfg)
            if bad:
                print("异常 #%-4s %-10s %s" % (b["id"], d["id"], "；".join(bad)))
                fail += 1
            else:
                print("通过 #%-4s %-10s %s" % (b["id"], d["id"], (b.get("title") or "")[:60]))
                ok += 1
        print("\n汇总：通过 %d / 异常 %d / 缺失 %d" % (ok, fail, missing))
        return 1 if (fail or missing) else 0

    # ── plan / apply ──
    bugs = zt.bugs(cfg["product_id"])
    by_title, by_bare, by_src = existing_index(bugs)
    print("登录 %s @ %s；产品 %s 现有缺陷 %d 条（查重基线）"
          % (sec["account"], cfg["base_url"], cfg["product_id"], len(bugs)))

    todo, skip = [], []
    force = getattr(args, "force", False)
    for d in defects:
        marked = build_payload(d, cfg, report_url)["title"]
        hit = by_src.get(d["id"]) or by_title.get(marked) or by_bare.get(normalize(marked))
        (skip if (hit and not force) else todo).append((d, hit) if hit else (d, None))
    pending = len(todo)
    if getattr(args, "limit", 0):
        todo = todo[:args.limit]

    for d, hit in skip:
        print("跳过 %-9s 已存在 → #%s %s" % (d["id"], hit.get("id"), (hit.get("title") or "")[:52]))
    print("\n待处理 %d 条 / 跳过 %d 条\n" % (len(todo), len(skip)))

    write = (args.cmd == "apply") and not args.dry_run
    if write and todo and not cfg.get("confirmed"):
        sys.exit("未完成入口确认，拒绝写入。\n"
                 "apply 会真实创建缺陷单，前置两道门不能省：\n"
                 "  1) auth  —— 向用户取得登录授权，确认用哪个账号\n"
                 "  2) meta  —— 检索真实枚举，用选项式交互确认关键字段\n"
                 "确认完成后，在 %s 里补 \"confirmed\": true（建议同时记 confirmed_by / confirmed_at / "
                 "confirmed_scope），再重跑本命令。\n"
                 "已确认过的环境只需做一次；换产品、换指派人才需要重新确认。"
                 % cfg.get("_from", "zentao_sync.config.json"))

    # 首次写入该环境：契约尚未验证，禁止一次批量——避免「N 条全废」的返工
    if write and todo and pending > 1 and not getattr(args, "limit", 0) and not force:
        logp0 = os.path.join(os.getcwd(), cfg.get("sync_log", ".push/sync.jsonl"))
        if not os.path.isfile(logp0) or os.path.getsize(logp0) == 0:
            sys.exit("首次写入该环境，拒绝批量创建 %d 条。\n"
                     "标题前缀、openedBuild 类型、steps 是否被转义都还没验证过，一次写这么多风险太高。\n"
                     "先执行：  apply --defects %s --limit 1\n"
                     "确认第 1 条回读校验通过后，再跑完整命令。" % (pending, args.defects))
    results = []
    for d, _ in todo:
        p = build_payload(d, cfg, report_url)
        print("--- %s %s  sev%s/pri%s  → %s" % (d["id"], d.get("lv"), p["severity"], p["pri"],
                                                p["assignedTo"] or "(不指派)"))
        print("    %s" % p["title"])
        if not write:
            print("    [dry-run] 未提交，steps %d 字符" % len(p["steps"]))
            results.append({"id": d["id"], "status": "dry-run"})
            continue
        st, r = zt.create(cfg["product_id"], p)
        bid = r.get("id") if isinstance(r, dict) else None
        if st not in (200, 201) or not bid:
            print("    -> HTTP %s 失败：%s" % (st, str(r)[:200]))
            results.append({"id": d["id"], "status": "failed", "http": st, "resp": str(r)[:300]})
            continue
        bad = verify_bug(d, zt.bug(bid), cfg)
        print("    -> HTTP %s 已创建 #%s；回读校验：%s" % (st, bid, "通过" if not bad else "；".join(bad)))
        results.append({"id": d["id"], "status": "created", "bug_id": bid, "verify": bad})

    if write and results:  # 全部跳过（todo 为空）时不落痕，避免日志里堆无意义的空记录
        logp = os.path.join(os.getcwd(), cfg.get("sync_log", ".push/sync.jsonl"))
        os.makedirs(os.path.dirname(logp) or ".", exist_ok=True)
        with io.open(logp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "product": cfg["product_id"], "defects_file": args.defects,
                                "result": results}, ensure_ascii=False) + "\n")
        print("\n留痕：%s" % logp)

    created = [r for r in results if r["status"] == "created"]
    failed = [r for r in results if r["status"] == "failed"]
    print("\n汇总：创建 %d / 跳过 %d / 失败 %d / dry-run %d"
          % (len(created), len(skip), len(failed),
             len([r for r in results if r["status"] == "dry-run"])))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
