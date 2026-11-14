# -*- coding: utf-8 -*-
"""禅道写入通用准则 · 规则 32（截图 / 证据必须直接内嵌）的**路径无关**门禁实现。

本模块刻意不含任何项目路径、禅道单号、图片文件映射，保证技能可跨项目使用。
若在下游分支中保留同源副本，请以本文件为准并保持两侧口径一致；
条款的完整说明见本技能 SKILL.md「截图与证据内嵌产出限制」。

规则 32 四条：
  ① 截图 / 证据区域必须直接内嵌真实图片，不允许只写文件名或「详见知识库报告」；
  ② 运行时证据必须完整给出，不允许截断后甩一句「完整证据见知识库报告」；
  ③ 所有「已内嵌于知识库报告 / 不重复嵌入大图 / 完整内容见知识库全量报告 /
     证据已截断 / 详情见知识库报告大图」等跳转措辞一律禁止；
  ④ 保留「来源报告」溯源（写入必携带报告），措辞改为
     「内容已完整内联于本单，团队无需跳转知识库查阅」。
"""
import re

# 禁止出现的「跳转 / 推脱」措辞。子串匹配即可，覆盖新旧两种写法。
FORBIDDEN_PHRASES = (
    "已内嵌于知识库报告",
    "不重复嵌入大图",
    "完整证据见知识库报告",
    "完整内容见知识库全量报告",
    "证据已截断",
    "详情见知识库报告",
    "详见知识库报告大图",
    "见知识库报告截图",
    "完整内容见知识库报告",
    "详见知识库全量报告",
    "大图见知识库",
)

# 需要改写为「已直接内嵌于本单」的旧溯源措辞
LEGACY_SOURCE_HINTS = (
    "（团队可在知识库查阅；本机路径不外链）",
    "（全量回归报告，内容已内联，本机路径不外链）",
    "（团队可在知识库查阅）",
)
SOURCE_HINT_NEW = "（内容已完整内联于本单，团队无需跳转知识库查阅）"

# 禅道把 base64 图落成附件后，steps 里可能出现三种等价引用形式：
#   写入时  data:image/png;base64,...
#   回读时  /index.php?m=file&f=read&t=png&fileID=N   或短宏  {N.png}
IMG_REF_PAT = re.compile(r'(data:image/png;base64,|fileID=\d+|\{\d+\.png\})')

# 「截图」表头 + 同行单元格（用于判定有没有真图）
SHOT_ROW_PAT = re.compile(
    r"<tr>\s*<th>截图[^<]*</th>\s*<td>(.*?)</td>\s*</tr>", re.S)
SHOT_TH_PAT = re.compile(r"<th>截图[^<]*</th>")

# 截断尾巴（规则 32 ②）：既拦中文省略号，也拦 JSON 半截结尾
TRUNCATION_PAT = re.compile(r'(…\s*（[^）]*(?:知识库|报告)[^）]*）)|(…\s*\(.*见.*\))')


def has_image_ref(s):
    """是否含任一形态的真实图片引用。"""
    return bool(IMG_REF_PAT.search(s or ""))


def find_forbidden(steps):
    """返回 steps 里命中的禁止措辞列表；空 = 合规。"""
    return [p for p in FORBIDDEN_PHRASES if p in (steps or "")]


def check_screenshot_rules(steps):
    """规则 32 门禁：返回拒绝原因字符串，合规返回 None。

    - 出现任何跳转 / 推脱 / 截断措辞 → 拒绝；
    - 出现「截图」表头但该行没有 <img> 图片引用 → 拒绝（不允许只有文件名或「参见他处」）；
    - 允许不含截图章节的 steps（例如纯文本历史单），由调用方决定是否额外要求图片。
    """
    s = steps or ""
    hits = find_forbidden(s)
    if hits:
        return "规则 32：steps 含禁止的跳转措辞 %s" % "、".join(hits)
    if TRUNCATION_PAT.search(s):
        return "规则 32：steps 含「证据已截断 / 完整内容见…」类尾巴，运行时证据必须完整给出"
    if SHOT_TH_PAT.search(s):
        rows = SHOT_ROW_PAT.findall(s)
        if not rows:
            return "规则 32：「截图」表头未落在标准表格行内，无法确认已内嵌真图"
        for cell in rows:
            if not ("<img" in cell and has_image_ref(cell)):
                return "规则 32：「截图」表格行未直接内嵌真实图片（不允许只写文件名或参见他处）"
    return None


def require_screenshot(steps):
    """更严格的产出侧检查：既过规则 32，又强制必须存在真图。

    用于新建缺陷 / 报告内联这类「本来就该带截图」的写入；
    历史单的纯文本 patch 用 check_screenshot_rules() 即可。
    """
    reason = check_screenshot_rules(steps)
    if reason:
        return reason
    if not has_image_ref(steps):
        return "规则 32：本次写入应包含直接内嵌的原始截图，steps 中未发现任何图片引用"
    return None
