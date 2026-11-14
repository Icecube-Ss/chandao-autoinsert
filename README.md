# chandao-autoinsert

把测试报告里的缺陷条目同步进禅道（ZenTao）缺陷库——**带复现步骤、影响面、修复建议的正式 Bug 单**，而不是一句「接口报错」。

面向的痛点是：跑完一轮测试，缺陷写在 HTML 报告里，开发看不到、跟不了、也不会去翻。手工往禅道里录一遍，二十条就是半小时，还容易漏、容易重复、容易把不同的两个 bug 写成同一条。

## 它解决什么

| 手工录单的问题 | 这里的做法 |
|---|---|
| 同一条缺陷录两遍 | 标题末尾 `[SRC:<缺陷号>]` 作幂等键，三级查重（标记 / 原文 / 归一化），重跑自动跳过 |
| 开发照着单子复现不出来 | 强制要求「接口 + 入参 + 次数 + 实际返回」，没有复现步骤不准建单 |
| 单子挂空、没人处理 | 指派人来自禅道真实账号枚举，不是猜的 |
| 环境问题被当成产品缺陷 | 环境阻塞与代码缺陷分开判定，宁可判阻塞也不污染质量结论 |
| 报告与缺陷库对不上 | `steps` 里带报告链接，形成双向追溯 |

缺陷标题统一为 `[<Agent缩写>-Px-X]-问题描述 [SRC:<缺陷号>]`，例如
`[ABC-P0-1]-示例查询接口 /api/v1/example/query 恒定返回 5xx [SRC:D-DEMO-01]`：
`ABC` 是实际执行者的短署名，`P0` 是严重程度，`1` 是优先级原值，末尾 `[SRC:]` 是防重复建单的幂等键。
**性能类（`type=performance`）另加类别标签**：`[ABC-P1-2]-【性能】示例查询接口 50 并发 P95 382.5ms 超线 [SRC:PERF-DEMO-01]`，
标签紧贴 `]-` 之后、置于描述之前，非性能类不得加（双向强制校验）。
格式细则见 `SKILL.md`「缺陷标题命名格式」。

## 亮点：把「该问的」做成脚本拦得住的门槛

这个技能不是 API 封装，它在写库前设了六道硬门：

**门一 · 登录授权**。`auth` 检测凭据来源并核验身份；没凭据就返回 `missing_credential` 并向用户索取，**不会翻别的目录找旧凭据顶替**。凭据存在 ≠ 用户这次同意用这个账号写缺陷。

**门一之二 · 传输风险确认**。`auth --json` 会随凭据状态一并返回 `risk` 块（`level` / `need_confirm` / 主机类型 / 风险说明）。
`http` 且不是回环地址时 `need_confirm=true`：**写操作前必须先拿到用户对明文传输的显式确认**，
否则 `apply` / `patch-steps` 直接拒绝执行，须带 `--accept-http-risk`（或临时 `export ZENTAO_SYNC_ACCEPT_HTTP_RISK=1`）。
私网 http 判定为「可控办公网内可接受」，公网 http 判定为「高危、明确劝阻」——两档话术不同，后者不提供「可忽略」选项。
`https` 与回环地址自动放行，不打扰。

**门一之三 · 配置只写仓库外**。`config.json` 受 Git 跟踪，`.gitignore` 拦不住已跟踪文件；
直接改它再 `git commit -a` 会把内网地址与账号推上远端。脚本读到被跟踪配置里含真实 `base_url` 时**直接阻断**，
`guard_config_target()` 拒绝写入该路径，配置一律用 `zentao_sync.config.json` 或 `--config` 指向仓库外。

**门二 · 字段确认**。`meta` 从禅道实时拉枚举——可见账号、历史接单人及次数、构建、`severity/pri/type` 实际取值分布，以及**哪些枚举接口根本不存在**。选项只能来自这里，不能凭印象。确认结果落成配置并置 `confirmed: true`，**没有这个标记 `apply` 拒绝写入**。

**门三 · 禅道写入通用准则（适用所有写入）**。写入前 `guard_steps_rules()` 校验
标题与 `steps`：标题必须符合 `[<Agent缩写>-Px-X]-…[SRC:x]`、幂等键固定末尾且 `steps` 首部保留同一份；
**性能类限定双向拒绝**——`type=performance` 缺 `【性能】` 标签或标签位置不对 → 拒，`type≠performance` 却带标签 → 同样拒
（`perf_title_reason()`，实现见 `scripts/zentao_sync.py`）。
`steps` 侧：只有目标单 `resolved` 且「回归已执行 + 根因已修复 + 状态与解决方案已实际变更为已解决」三条证据齐全，
才允许结论块叫 `【代码级验证结论 · 闭环】`；其余一律 `【修复建议】`。新建单恒 `active`，因此**建单路径永远写不进闭环标题**，
伪造 `closure_gate` 也会被拒；`patch-steps` 按目标单真实状态判定，只改 `title` 的 `PUT` 会自动读回真实 `type` 与现有 `steps` 送校验
（提交仍只带 `title`），`verify` 回读复查标题 / 闭环 / 截图 / 性能标签。总纲见 `SKILL.md`「禅道写入准则（通用）」。

**门四 · 截图 / 证据必须直接内嵌（适用所有写入）**。`guard_steps_rules()` 同时校验 `steps`：
报告全文与**完整**运行时证据直接内联；只要出现「截图」表头就必须在同行贴入原始 PNG（`<img src="data:image/png;base64,…">`，
禅道自动转附件并在页面显示）；禁止 `已内嵌于知识库报告` / `不重复嵌入大图` / `完整证据见知识库报告` /
`证据已截断` / `详情见知识库报告大图` 等把开发推去知识库看大图的话术。
判定在 `scripts/shot_rules.py`，回读 `verify` 复查三种图片引用形态
（`data:image/png;base64,…` / `fileID=N` / `{N.png}`）。详见 `SKILL.md`「截图与证据内嵌产出限制」。

再加一道：该环境首次写入若一次超过 1 条、又没加 `--limit`，直接拒绝——逼你先单条验证契约。这条来自真实事故：`steps` 里的 `<a>` 被二次转义成 `&lt;a href="<a href=…`，批量写完才发现 N 条全废。

## 安装

纯标准库，**无需 pip install**，Python 3.7+。

```bash
git clone https://github.com/Icecube-Ss/chandao-autoinsert.git \
    ~/.claude/skills/chandao-autoinsert
# 或 WorkBuddy：~/.workbuddy/skills/chandao-autoinsert
# 或任意 agent 框架：把目录放到它的 skills 目录即可
```

SKILL.md 的 frontmatter 是标准 `name` + `description`，任何识别 Agent Skills 规范的工具都能加载。

## 快速开始

```bash
S=~/.claude/skills/chandao-autoinsert/scripts/zentao_sync.py

# 1. 配地址（口令不要写这里）
export ZENTAO_ACCOUNT=your-account
export ZENTAO_PASSWORD=your-password
# 禅道地址写进当前工作目录的 zentao_sync.config.json，或 --config 指定

# 2. 核验授权（无凭据会提示去要；返回里带 risk 块，need_confirm 时先向用户确认传输风险）
python $S auth --json

# 3. 拉真实枚举，据此与用户确认指派人/等级/类型/构建
python $S meta --json

# 4. 看基线
python $S list

# 5. 演练（不发写请求）
python $S plan --defects defects/my-report.json

# 6. 单条试写，验证契约（明文 HTTP 需先取得用户确认，再加 --accept-http-risk）
python $S apply --defects defects/my-report.json --limit 1 --accept-http-risk

# 7. 批量补齐（自动跳过已建）
python $S apply --defects defects/my-report.json

# 8. 回读校验
python $S verify --defects defects/my-report.json
```

缺陷文件格式见 `references/defect-schema.md`，模板可直接复制 `assets/defects-template.json`。

## 命令

| 命令 | 作用 | 写库 |
|---|---|---|
| `auth [--json]` | 登录授权核验 | 否 |
| `meta [--json]` | 字段枚举检索 + 接口能力探测 | 否 |
| `list` | 列现有缺陷 | 否 |
| `plan --defects F` | 打印将要提交的 payload | 否 |
| `apply --defects F [--limit N] [--force] [--accept-http-risk]` | 创建 + 写后自动回读校验 | **是** |
| `verify --defects F` | 按缺陷文件回读校验 | 否 |
| `patch-steps --defects F --bug <单号> [--accept-http-risk]` | 用最新渲染逻辑重写某单 steps | **是** |

## 兼容性

- Python 3.7+ / Windows、macOS、Linux
- 终端编码：已强制 UTF-8 输出并切换 Windows 代码页，GBK 与 `LC_ALL=C` 下实测正常
- 配置文件：UTF-8 带 BOM 也能读（Windows 记事本存的也行）
- 自签证书：`ZENTAO_SYNC_INSECURE=1` 跳过校验（默认严格校验，不静默弱化）。**该开关只对 `https` 生效**——
  `http` 连接没有 TLS 层，设了也不起作用。它**会完全关闭证书验证**（不验证签发者、有效期、域名），
  仅适合完全可信的内网自建实例临时使用；对公网可达地址开启会引入中间人风险。正解是把内部 CA
  导入系统信任库，而不是关校验。
- 明文 HTTP：技能会在 `auth` 阶段返回风险等级，并在写操作前要求显式确认（`--accept-http-risk`）。
  私网环境属可控风险，公网环境请改用 https。
- 宿主无选项式提问工具时，按 SKILL.md 的降级方式输出编号列表
- 禅道版本差异：`references/api-contract.md` 是实测契约，**换实例请先跑 `meta` 重新探测**，别照抄 404 结论

## 目录结构

```
SKILL.md                      流程、六道硬门、七步工作流、禅道写入准则（通用）、宿主差异表
config.json                   示例配置（占位值，勿直接用）
scripts/zentao_sync.py        全部实现，纯标准库
scripts/shot_rules.py         截图 / 证据内嵌门禁（路径无关，可独立复用）
references/api-contract.md    实测 API 契约（哪些接口有、哪些 404、字段真实行为）
references/defect-schema.md   缺陷输入格式与硬规矩
assets/defects-template.json  填空模板
```

## 已知限制

- 禅道 REST v1 没有评论接口，后续进展只能改 `steps` 正文
- 该实例 `module` / `execution` 无枚举接口，只能写 0，缺陷无法按模块归类（可用标题前缀区分）
- `?keyword=` 不生效，查重全靠客户端拉全量比对；缺陷量到万级会变慢
- multipart 直传附件不支持（`/api.php/v1/files` 返回「文件格式不在规定范围内」）；但**把 `<img src="data:image/png;base64,…">` 写进 `steps` 会让禅道自动生成真实附件并在页面显示**（截图内嵌门禁采用此路径）

## License

MIT
