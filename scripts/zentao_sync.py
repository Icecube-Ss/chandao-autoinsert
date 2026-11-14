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
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import shot_rules  # 截图 / 证据必须直接内嵌（规则 32 门禁）
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


TRACKED_CONFIG = os.path.join(SKILL_DIR, "config.json")


def guard_config_target(path):
    """禁止把真实环境配置写进被 Git 跟踪的 config.json。

    config.json 是仓库里的示例配置，**受版本控制**；
    .gitignore 只能拦 `zentao_sync.config.json`（带前缀），拦不住已跟踪文件。
    若有人图省事直接改它，一次 `git commit -a` 就会把内网地址、指派人账号推上远端。
    故写入边界由代码守住：技能目录内的 config.json 只读，配置一律另存为带前缀的文件。
    """
    if not path:
        return path
    try:
        same = os.path.normcase(os.path.realpath(path)) == \
            os.path.normcase(os.path.realpath(TRACKED_CONFIG))
    except Exception:
        same = False
    if same:
        sys.exit(
            "已拒绝写入 %s：该文件是仓库内的示例配置，受 Git 跟踪，"
            "写入后一旦提交就会把内网地址与账号推到远端。\n"
            "请改用：当前工作目录下的 zentao_sync.config.json（已在 .gitignore 中），"
            "或用 --config 显式指定一个仓库外的路径。\n"
            "示例：  --config ~/zentao/zentao_sync.config.json" % path)
    return path


def secure_config_hint(current_path):
    """提示当前配置来源是否安全（只用于 meta/auth 的输出，不阻断）。"""
    try:
        if current_path and os.path.normcase(os.path.realpath(current_path)) == \
                os.path.normcase(os.path.realpath(TRACKED_CONFIG)):
            return ("warning_tracked_config",
                    "当前读的是仓库内示例配置 config.json。若你把真实地址写在这里，"
                    "提交时会泄露内网信息；请改写到 zentao_sync.config.json 或 --config 指向的仓库外路径。")
    except Exception:
        pass
    return None, None


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


# ────────────────────────── 传输风险（明文 HTTP）──────────────────────────
#
# 本工具登录时以明文发送账号口令（POST /api.php/v1/tokens），随后所有请求带明文
# Token。HTTP 下这两样都在网络层裸奔：同广播域的主动 ARP 欺骗、恶意 AP、端口镜像
# 都能截获，攻击者拿到口令即可冒用身份。HTTPS 则有 TLS 保护，风险显著降低。
#
# 因此按「传输强度 + 网络位置」分四级，写操作前必须拿到用户对该等级的显式确认：
#   https        —— 有 TLS，无需确认
#   回环/本机     —— 流量不出主机，无需确认
#   私网 http     —— 可控办公网内可接受，但需确认一次（提醒不要在不可信网络跑）
#   公网 http     —— 高危，需确认，且确认语明确劝阻
#
# 确认方式（二者其一，均需显式动作，不做静默放行）：
#   --accept-http-risk      本次命令放行
#   ZENTAO_SYNC_ACCEPT_HTTP_RISK=1   显式接受（建议仅在本机 shell 会话内 export）

RISK_LEVELS = {
    "https": {"level": "none", "need_confirm": False},
    "loopback": {"level": "low", "need_confirm": False},
    "private_http": {"level": "medium", "need_confirm": True},
    "public_http": {"level": "high", "need_confirm": True},
}

ACCEPT_RISK_ENV = "ZENTAO_SYNC_ACCEPT_HTTP_RISK"


def _is_loopback(host):
    h = (host or "").strip().lower().strip("[]")
    if h in ("localhost", "::1", "0.0.0.0"):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(h).is_loopback
    except Exception:
        return False


def _is_private(host):
    """私网 / 链路本地 / 唯一本地地址判定；域名一律按 public 处理（保守）。"""
    h = (host or "").strip().lower().strip("[]")
    if h.endswith(".local") or h.endswith(".lan") or h.endswith(".internal"):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_link_local
    except Exception:
        return False


def transport_risk(base_url):
    """判定与禅道交互的传输风险等级。

    返回 dict：level / need_confirm / scheme / host_class / reason / advice
    host_class 只输出「回环 / 私网 / 公网」这类分类，不回显真实主机名或 IP，
    避免把内网地址写进日志、报告或对话记录。
    """
    u = (base_url or "").strip()
    scheme = (u.split("://", 1)[0].lower() if "://" in u else "")
    host = ""
    if "://" in u:
        rest = u.split("://", 1)[1]
        host = rest.split("/", 1)[0]
        if "@" in host:                      # 去掉 userinfo
            host = host.rsplit("@", 1)[1]
        if host.startswith("["):             # IPv6 字面量
            host = host[1:].split("]", 1)[0]
        elif ":" in host:                    # 去端口
            host = host.rsplit(":", 1)[0]

    if scheme == "https":
        info = dict(RISK_LEVELS["https"])
        info.update(scheme="https", host_class="不适用", reason="已启用 TLS，链路加密。", advice="")
        return info

    if scheme != "http":
        info = dict(RISK_LEVELS["public_http"])
        info.update(scheme=scheme or "未知", host_class="未知",
                    reason="无法识别协议（%s），按最高风险处理。" % (scheme or "空"),
                    advice="请确认 base_url 形如 http://host:port/ 或 https://host/")
        return info

    if _is_loopback(host):
        info = dict(RISK_LEVELS["loopback"])
        info.update(scheme="http", host_class="回环/本机",
                    reason="目标为本机回环地址，流量不出主机，明文无外部暴露面。", advice="")
        return info

    if _is_private(host):
        info = dict(RISK_LEVELS["private_http"])
        info.update(scheme="http", host_class="私网",
                    reason="以明文 HTTP 与私网禅道交互：登录口令与 Token 在网络上可被截获。"
                           "同广播域的主动 ARP 欺骗、恶意接入点或端口镜像都可利用；"
                           "被动嗅探在现代交换网络下通常听不到他人单播流量。",
                    advice="可控的办公网内可接受；不要在访客 Wi-Fi、公共网络或不可信网段运行。"
                           "条件允许时改用 https 并导入内部 CA。")
        return info

    info = dict(RISK_LEVELS["public_http"])
    info.update(scheme="http", host_class="公网",
                reason="以明文 HTTP 与公网可达的禅道交互：口令与 Token 会经过路径上每一跳，"
                       "存在被截获与被冒充的双重风险。此类暴露面通常数小时内即被全网扫描到。",
                advice="强烈建议改用 https（受信 CA 或内部 CA）。不要为绕过证书错误而关闭校验。")
    return info


def risk_accepted(args=None):
    """用户是否已显式接受本次明文传输风险。"""
    if args is not None and getattr(args, "accept_http_risk", False):
        return True
    return os.environ.get(ACCEPT_RISK_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def http_risk_prompt(risk):
    """生成给人看 / 给 Agent 转述的确认文案。"""
    lvl = risk.get("level")
    if lvl == "high":
        head = "⚠️ 高危：当前与禅道的连接是明文 HTTP，且目标是公网可达地址。"
    else:
        head = "注意：当前与禅道的连接是明文 HTTP（无 TLS 加密）。"
    return (
        "%s\n"
        "  目标类型：%s（%s）\n"
        "  风险说明：%s\n"
        "  规避建议：%s\n"
        "  若确认继续，请显式加 --accept-http-risk（仅本次生效），"
        "或在可信会话内 export %s=1。"
        % (head, risk.get("host_class"), risk.get("scheme"),
           risk.get("reason"), risk.get("advice"), ACCEPT_RISK_ENV)
    )


def require_transport_ack(risk, args, action="写入"):
    """写操作前的强制闸门：明文传输且用户未显式确认时拒绝执行。

    https / 回环直接放行；私网与公网 http 必须由调用方拿到显式确认后放行。
    """
    if not risk.get("need_confirm"):
        return True
    if risk_accepted(args):
        return True
    sys.exit("已拒绝%s：%s\n\n%s"
             % (action, "明文 HTTP 传输未获确认", http_risk_prompt(risk)))


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

    def bugs(self, product_id, limit=1000, status="all"):
        """产品缺陷列表 = 查重基线，**默认全状态口径 `status=all`**（2026-11-13 用户确认修）。

        禅道 `GET /products/{id}/bugs` **不带 `status` 时默认排除 `closed`**（实测：默认 89 条 /
        `status=all` 120 条，隐藏的 31 条逐条核实全部 `closed` 且 `deleted=false`，不是删除）。
        沿用默认口径会让查重基线漏掉已关闭单 → **同一缺陷重复建单**、`verify` 误报「缺失」。
        同一接口加不加该参数**返回字段形态完全一致**，故 `existing_index` / `normalize` 无需改。
        需要旧口径（只看未关闭单）时显式传 `status=""`。
        """
        q = "?limit=%d" % limit + ("&status=%s" % status if status else "")
        st, r = self.get("/api.php/v1/products/%s/bugs%s" % (product_id, q))
        if st != 200:
            raise SystemExit("拉取缺陷列表失败：HTTP %s %s" % (st, str(r)[:200]))
        return r.get("bugs", [])

    def bug(self, bug_id):
        st, r = self.get("/api.php/v1/bugs/%s" % bug_id)
        return (r if st == 200 and isinstance(r, dict) else None)

    def create(self, product_id, payload):
        # 禅道写入通用准则：新建单恒为 active，不可能满足「已解决」条件 → 硬门禁
        guard_steps_rules(payload, "active", False)
        return http_json("%s/api.php/v1/products/%s/bugs" % (self.base, product_id),
                         "POST", payload, token=self.token)

    def patch(self, bug_id, payload, target_status=None, gate_ok=False, target_type=None):
        # 禅道写入通用准则：按目标单真实状态 / 真实 type 判定；调用方未传则实时读一次，防止绕过。
        # 只改 title 的 PUT 同样过门禁：把目标单现有 steps 一并送校验（幂等键 / 截图 / 结论块），
        # 实际提交仍只带调用方给的字段，不连带改 steps。
        p = payload or {}
        if "steps" in p or "title" in p:
            st, tt = target_status, target_type
            need_snap = (st is None) or ("title" in p and (tt is None or "steps" not in p))
            snap = self.bug(bug_id) if need_snap else None
            if st is None:
                st = (snap or {}).get("status") or "active"
            if tt is None:
                tt = (snap or {}).get("type") or ""
            check = dict(p)
            if "title" in p and "steps" not in p:
                check["steps"] = (snap or {}).get("steps") or ""
            guard_steps_rules(check, st, gate_ok, tt)
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
    """归一化标题用于跨来源比对：剥离格式前缀、任意位置的 [SRC:xxx] 幂等键与【性能】标签。

    同时兼容四代标题：
      旧版  ``[SRC:x] 问题描述``
      新版  ``[AGENT-Px-X]-问题描述 [SRC:x]``
      性能版 ``[AGENT-Px-X]-【性能】问题描述 [SRC:x]``（性能类限定）
      纯文本 ``问题描述``
    幂等键改到标题末尾后，旧实现会把它之后的内容全部截掉（返回空串），
    导致 by_bare 索引失效；这里改为「剥离标记」而非「截断到标记之后」。
    【性能】同样剥离，保证同一缺陷「加标签前 / 后」两种标题在 by_bare 互相命中。
    """
    t = title or ""
    t = re.sub(r"\[SRC:[^\]]*\]", "", t)
    t = re.sub(r"^\s*\[[A-Za-z][A-Za-z0-9]*-P\d-\d+\]-\s*", "", t)
    t = re.sub(r"^\s*" + re.escape(PERF_TAG) + r"\s*", "", t)
    return " ".join(t.replace("\u3000", " ").split()).lower()


# ───────── 禅道写入通用准则 / 结论块标题（强制）─────────
# 适用所有禅道写入，详见 SKILL.md「禅道写入准则（通用）」与 references/defect-schema.md。
# 只有目标单 status=resolved 且三条件证据齐全，才允许把结论块叫「代码级验证结论 · 闭环」；
# 其余（含新建单，恒 active）一律「【修复建议】」。

CLOSURE_TITLE = "【代码级验证结论 · 闭环】"
FIX_TITLE = "【修复建议】"
CLOSURE_TITLE_PAT = re.compile(r"【[^】]*代码级验证结论[^】]*】")

# ───────── 性能类标题限定（强制 · 通用）─────────
# type=performance 的写入，标题一律 [<Agent缩写>-Px-X]-【性能】问题描述 [SRC:<缺陷号>]；
# 标签紧贴 ]- 之后、置于问题描述之前，三段前缀与末尾幂等键一字不改；非性能类不得加。
# 若下游分支保留同源副本，请以本文件为准并保持两侧口径一致。
PERF_TAG = "【性能】"
PERF_TYPE = "performance"
TITLE_PREFIX_PAT = r"^\[[A-Za-z0-9]+-P[0-3]-[1-4]\]-"


def perf_title_reason(title, btype):
    """规则 28「性能类限定」双向校验：type=performance ↔ 标题前缀后紧跟【性能】。

    返回 None = 合规；返回字符串 = 拒绝原因。
    btype 取本次写入的真实 type（新建取 payload['type']，只改 title 的 PUT 由调用方
    读回目标单 type 传入）；btype 为空/未知时只校验标签形态，不做双向判定，
    以免误伤纯 steps 回填等拿不到 type 的历史调用。
    """
    t = str(btype or "").strip().lower()
    has = PERF_TAG in (title or "")
    at = bool(re.match(TITLE_PREFIX_PAT + re.escape(PERF_TAG), title or ""))
    if t == PERF_TYPE:
        if not has:
            return ("性能类（type=performance）标题缺少【性能】标签，须为 "
                    "[<Agent缩写>-Px-X]-【性能】问题描述 [SRC:<缺陷号>]（规则 28 性能类限定）")
        if not at:
            return ("【性能】标签位置错误：必须紧贴 ]- 之后、置于问题描述之前，"
                    "不得移到末尾或写进 [SRC:]（规则 28 性能类限定）")
    elif t:
        if has:
            return "非性能类（type=%s）标题不得带【性能】标签（规则 28 性能类限定）" % t
    elif has and not at:
        return "【性能】标签位置错误：必须紧贴 ]- 之后（规则 28 性能类限定）"
    return None


def fit_title(title, limit=200):
    """标题长度上限 200：超限时只截问题描述，**保住末尾幂等键 [SRC:]**（规则 28 不得删除）。"""
    if len(title) <= limit:
        return title
    m = re.search(r"\s*\[SRC:[^\]]*\]\s*$", title)
    if not m:
        return title[:limit]
    key = title[m.start():]
    return title[:limit - len(key)].rstrip() + key


def closure_gate_ok(d):
    """三条件全成立且证据齐全才返回 True：
    ① 回归测试已执行 ② 根因对应已修复 ③ 状态与解决方案已实际变更为已解决。
    resolve_verified 只能在 resolve 成功并回读 status=resolved 之后填 True，
    「打算改状态」不算。缺省 / 缺字段 / 缺证据 → False → 降级为「【修复建议】」。
    """
    g = d.get("closure_gate") or {}
    if not isinstance(g, dict):
        return False
    flags = (g.get("regression_executed") is True
             and g.get("root_cause_fixed") is True
             and g.get("resolve_bug") is True
             and g.get("resolve_verified") is True)
    evid = all(str(g.get(k) or "").strip() for k in
               ("regression_evidence", "fix_evidence", "resolution"))
    return bool(flags and evid)


def closure_allowed(d, target_status="active"):
    """双闸门：目标单真实状态 + 三条件证据。新建单 target_status='active' → 恒 False。"""
    return str(target_status or "") == "resolved" and closure_gate_ok(d)


def conclusion_title(d, target_status="active"):
    return CLOSURE_TITLE if closure_allowed(d, target_status) else FIX_TITLE


def conclusion_html(d, target_status="active"):
    """结论块富文本；标题按目标状态 + gate 条件化，正文不受影响。"""
    c = d.get("closure") or d.get("conclusion")
    if not c:
        return ""
    audit = ""
    if closure_allowed(d, target_status):
        g = d["closure_gate"]
        audit = ("<p>闭环口径：回归已执行（%s）；根因已修复（%s）；"
                 "本单状态与解决方案已变更为已解决（%s）。</p>"
                 % (html.escape(str(g["regression_evidence"])),
                    html.escape(str(g["fix_evidence"])),
                    html.escape(str(g["resolution"]))))
    if isinstance(c, list):
        body = "<ol>%s</ol>" % "".join("<li>%s</li>" % html.escape(str(x)) for x in c)
    else:
        body = "<p>%s</p>" % html.escape(str(c))
    return "<p><b>%s</b></p>%s%s" % (conclusion_title(d, target_status), audit, body)


def guard_steps_rules(payload, target_status, gate_ok=False, target_type=None):
    """所有禅道写入的底层门禁（规则 28 标题格式 + 性能类限定 / 规则 30 结论块双闸门 / 规则 32 截图证据内嵌）。

    - 规则 28：带 title 的写入必须符合 `[<Agent缩写>-Px-X]-问题描述 [SRC:x]`，
      `[SRC:]` 固定末尾且 steps 首部保留同一份；**type=performance 必须带【性能】标签，
      非性能类不得带**（2026-11-13 性能类限定，双向拒绝）。
    - 规则 30：steps 含「【…代码级验证结论…】」而目标单不是 resolved、
      或三条件证据不齐 → 直接拒绝写入。
    - 规则 32：steps 含「详见知识库报告大图 / 证据已截断」等跳转措辞，
      或出现「截图」表头却没内嵌真图 → 直接拒绝写入。
    target_type：只改 title 的 PUT 必须传目标单真实 type（payload 里没有 type），
    否则性能类双向判定无从做起；未传时只校验标签形态。
    不静默改文案：让调用方修产出，而不是让脚本替它猜。
    """
    payload = payload or {}
    steps = payload.get("steps") or ""
    title = (payload.get("title") or "").strip()
    shot_reason = shot_rules.check_screenshot_rules(steps)
    if shot_reason:
        raise SystemExit(
            "拒绝写入：%s。\n"
            "禅道写入通用准则 · 规则 32：截图区域必须直接内嵌原始图片，"
            "运行时证据必须完整；不得用「详见知识库报告大图」「完整证据见知识库报告」"
            "「证据已截断」等措辞把开发推到知识库。"
            % shot_reason)
    if title:
        if not re.match(TITLE_PREFIX_PAT, title):
            raise SystemExit(
                "拒绝写入：标题未按规则 28 生成 `[<Agent缩写>-Px-X]-问题描述 [SRC:<缺陷号>]`：%s\n"
                "标题由 build_payload() → title_text() 统一生成，不要手工拼。" % title)
        if "[SRC:" not in title:
            raise SystemExit("拒绝写入：标题缺少末尾幂等键 [SRC:<缺陷号>]（规则 28）：%s" % title)
        src = title.rsplit("[SRC:", 1)[-1].strip().rstrip("]").strip()
        if not src:
            raise SystemExit("拒绝写入：幂等键 [SRC:] 内容为空（规则 28）：%s" % title)
        if ("[SRC:%s]" % src) not in steps:
            raise SystemExit(
                "拒绝写入：steps 缺少同一幂等键 [SRC:%s]（规则 28：steps 首部必须保留）。\n"
                "只改 title 的 PUT 请把目标单现有 steps 一并传入门禁校验，实际提交仍只带 title。" % src)
        perf_reason = perf_title_reason(title, payload.get("type") or target_type)
        if perf_reason:
            raise SystemExit("拒绝写入：%s\n当前标题：%s" % (perf_reason, title))
    m = CLOSURE_TITLE_PAT.search(steps)
    if m and not (str(target_status or "") == "resolved" and gate_ok is True):
        raise SystemExit(
            "拒绝写入：steps 含「%s」，但目标单 status=%s、三条件证据 gate_ok=%s。\n"
            "禅道写入通用准则：只有「① 回归测试已执行 ② 根因对应已修复 "
            "③ 状态与解决方案已实际变更为已解决」三条同时成立，才允许闭环标题；\n"
            "其余一律用「%s」。新建单恒为 active，不允许写入闭环标题"
            "（即使在 defects 里伪造 closure_gate 也不放行）。"
            % (m.group(0), target_status, gate_ok, FIX_TITLE))
    return payload


def steps_html(d, report_url="", target_status="active"):
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
    ch = conclusion_html(d, target_status)
    if ch.strip():
        parts.append("<hr />")
        parts.append(ch)
    return "".join(parts)


def signature(d):
    """幂等键 [SRC:<缺陷号>]：固定置于标题末尾，steps 首行也保留同一份。"""
    return "[SRC:%s]" % d["id"]


TITLE_LV = {"P0": "P0", "P1": "P1", "P2": "P2", "P3": "P3", "P4": "P3",
            "1": "P0", "2": "P1", "3": "P2", "4": "P3"}


def defect_type(d, cfg):
    """本次写入的真实 type：缺陷自带 d['type'] > cfg['default_type'] > codeerror。

    标题的【性能】标签与 payload['type'] 必须取同一个值，否则会出现
    「带标签但 type 不是 performance」被门禁拒（规则 28 性能类限定）。
    """
    return str(d.get("type") or cfg.get("default_type", "codeerror")).strip().lower()


def title_text(d, cfg):
    """统一标题格式：[<Agent缩写>-Px-X]-问题描述 [SRC:<缺陷号>]
    性能类（type=performance）：[<Agent缩写>-Px-X]-【性能】问题描述 [SRC:<缺陷号>]

    Px = 严重程度（severity 1/2/3/4 → P0/P1/P2/P3）；X = 优先级 pri 原值（不带 P）。
    agent_short 由配置给出（例如 ABC），缺省回退 AGENT，不写完整署名。
    幂等键在末尾：existing_index.by_src 位置无关，verify_bug 用本函数回比。
    """
    lv = str(d.get("lv", "P2")).upper()
    px = TITLE_LV.get(lv, "P2")
    x = cfg.get("pri_map", {}).get(lv, 3)
    short = str(cfg.get("agent_short") or d.get("agent_short") or "AGENT").strip()
    desc = d.get("title", "")
    if defect_type(d, cfg) == PERF_TYPE and PERF_TAG not in desc:
        desc = PERF_TAG + desc
    return fit_title("[%s-%s-%s]-%s %s" % (short, px, x, desc, signature(d)))


def build_payload(d, cfg, report_url=""):
    lv = d.get("lv", "P2")
    return {
        "title": title_text(d, cfg),
        "severity": cfg["severity_map"].get(lv, 3),
        "pri": cfg["pri_map"].get(lv, 3),
        "type": defect_type(d, cfg),
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
    # 禅道写入通用准则：未 resolved 的单不允许带闭环标题（回读侧同样卡一道）
    if CLOSURE_TITLE_PAT.search(snap.get("steps") or "") and snap.get("status") != "resolved":
        bad.append("steps 含闭环标题但单未 resolved（违反禅道写入通用准则）")
    # 规则 32：回读侧复核。禅道会把 base64 图转成 fileID=N 或 {N.png}，三种形态都算真图
    shot_reason = shot_rules.check_screenshot_rules(snap.get("steps") or "")
    if shot_reason:
        bad.append(shot_reason + "（回读）")
    # 规则 28 性能类限定：回读侧按目标单真实 type 复核【性能】标签（双向）
    perf_reason = perf_title_reason(snap.get("title") or "", snap.get("type"))
    if perf_reason:
        bad.append(perf_reason + "（回读）")
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
    print("当前产品：%s %s（现有缺陷 %d 条 · 全状态含已关闭）" % (m["product_id"], m["product_name"], m["bug_count"]))
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
    p.add_argument("--accept-http-risk", action="store_true", dest="accept_http_risk",
                   help="确认接受明文 HTTP 传输风险（仅本次命令生效）", **kw)
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
                 "注意：不要修改技能目录里的 config.json——它受 Git 跟踪，写了真实地址会被提交泄露。\n"
                 "配好后依次跑：auth 核验授权（含明文传输风险提示）→ meta 检索字段枚举 → 与用户确认。"
                 % cfg.get("_from", "内置默认"))

    # 被 Git 跟踪的 config.json 里出现真实地址 = 即将泄露内网信息，直接阻断
    _warn, _msg = secure_config_hint(cfg.get("_from"))
    if _warn == "warning_tracked_config":
        sys.exit("已阻断：%s\n当前读到 base_url 的来源是 %s。"
                 % (_msg, cfg.get("_from")))

    # ── auth：无凭据也要能跑，用来告诉调用方「该向用户索取授权了」──
    if args.cmd == "auth":
        risk = transport_risk(cfg.get("base_url"))
        sec, src = resolve_secret(getattr(args, "secret", None), required=False)
        if not sec:
            if args.json:
                print(json.dumps({"ok": False, "error": "missing_credential",
                                  "hint": MISSING_SECRET, "risk": risk,
                                  "risk_accepted": risk_accepted(args)},
                                 ensure_ascii=False, indent=2))
            else:
                print(MISSING_SECRET)
                if risk.get("need_confirm"):
                    print("\n" + http_risk_prompt(risk))
            return 2
        out, _ = do_auth(cfg, sec, src)
        # 传输风险随 auth 一并返回：交互层据此在「索取凭据」之前就能提示用户
        out["risk"] = risk
        out["risk_accepted"] = risk_accepted(args)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print_auth(out)
            if risk.get("need_confirm") and not risk_accepted(args):
                print("\n" + http_risk_prompt(risk))
        return 0 if out.get("ok") else 2

    if not cfg.get("product_id"):
        sys.exit("配置缺 product_id：用 --config 指定，或先跑 meta 看产品列表后写入配置")

    risk = transport_risk(cfg.get("base_url"))

    # 写操作前必须先拿到明文传输的显式确认（https / 回环自动放行）
    if args.cmd in ("apply", "patch-steps"):
        require_transport_ack(risk, args, action="写入禅道")

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
        print("产品 %s 共 %d 条缺陷（全状态含已关闭）" % (cfg["product_id"], len(bugs)))
        for b in sorted(bugs, key=lambda x: x.get("id", 0)):
            print("%5s | %-8s | sev%s | %s" % (b.get("id"), b.get("status"), b.get("severity"),
                                               (b.get("title") or "")[:88]))
        return 0

    defects, report_url = load_defects(args.defects)
    if not defects:
        sys.exit("缺陷文件里没有 defects 条目：%s" % args.defects)

    # ── patch-steps ──
    if args.cmd == "patch-steps":
        st0, snap0 = zt.get("/api.php/v1/bugs/%s" % args.bug)
        snap = snap0 if st0 == 200 and isinstance(snap0, dict) else None
        if not snap:
            sys.exit("读不到单 #%s（HTTP %s），拒绝 patch" % (args.bug, st0))
        target_status = snap.get("status") or "active"
        hit = next((d for d in defects
                    if ("[SRC:%s]" % d["id"]) in (snap.get("title") or "")), None)
        if not hit:
            sys.exit("单 #%s 的标题里找不到匹配的 [SRC:xxx] 标记，无法确定对应哪条缺陷" % args.bug)
        new_steps = steps_html(hit, report_url, target_status=target_status)
        st, r = zt.patch(args.bug, {"steps": new_steps},
                         target_status=target_status, gate_ok=closure_gate_ok(hit))
        print("patch #%s ← %s : HTTP %s（目标状态 %s）" % (args.bug, hit["id"], st, target_status))
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
    print("登录 %s @ %s；产品 %s 现有缺陷 %d 条（查重基线 · 全状态含已关闭）"
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
        print("    写入准则: target_status=active（新建）· 结论块标题=%s · create 门禁已启用"
              % conclusion_title(d, "active"))
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
