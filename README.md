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
格式细则见 `SKILL.md`「缺陷标题命名格式」。

## 亮点：把「该问的」做成脚本拦得住的门槛

这个技能不是 API 封装，它在写库前设了两道硬门：

**门一 · 登录授权**。`auth` 检测凭据来源并核验身份；没凭据就返回 `missing_credential` 并向用户索取，**不会翻别的目录找旧凭据顶替**。凭据存在 ≠ 用户这次同意用这个账号写缺陷。

**门二 · 字段确认**。`meta` 从禅道实时拉枚举——可见账号、历史接单人及次数、构建、`severity/pri/type` 实际取值分布，以及**哪些枚举接口根本不存在**。选项只能来自这里，不能凭印象。确认结果落成配置并置 `confirmed: true`，**没有这个标记 `apply` 拒绝写入**。

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

# 2. 核验授权（无凭据会提示去要）
python $S auth --json

# 3. 拉真实枚举，据此与用户确认指派人/等级/类型/构建
python $S meta --json

# 4. 看基线
python $S list

# 5. 演练（不发写请求）
python $S plan --defects defects/my-report.json

# 6. 单条试写，验证契约
python $S apply --defects defects/my-report.json --limit 1

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
| `apply --defects F [--limit N] [--force]` | 创建 + 写后自动回读校验 | **是** |
| `verify --defects F` | 按缺陷文件回读校验 | 否 |
| `patch-steps --defects F --bug 48` | 用最新渲染逻辑重写某单 steps | **是** |

## 兼容性

- Python 3.7+ / Windows、macOS、Linux
- 终端编码：已强制 UTF-8 输出并切换 Windows 代码页，GBK 与 `LC_ALL=C` 下实测正常
- 配置文件：UTF-8 带 BOM 也能读（Windows 记事本存的也行）
- 自签证书：`ZENTAO_SYNC_INSECURE=1` 跳过校验（默认严格校验，不静默弱化）
- 宿主无选项式提问工具时，按 SKILL.md 的降级方式输出编号列表
- 禅道版本差异：`references/api-contract.md` 是实测契约，**换实例请先跑 `meta` 重新探测**，别照抄 404 结论

## 目录结构

```
SKILL.md                      流程、两道门、七步工作流、宿主差异表
config.json                   示例配置（占位值，勿直接用）
scripts/zentao_sync.py        全部实现，纯标准库
references/api-contract.md    实测 API 契约（哪些接口有、哪些 404、字段真实行为）
references/defect-schema.md   缺陷输入格式与三条硬规矩
assets/defects-template.json  填空模板
```

## 已知限制

- 禅道 REST v1 没有评论接口，后续进展只能改 `steps` 正文
- 该实例 `module` / `execution` 无枚举接口，只能写 0，缺陷无法按模块归类（可用标题前缀区分）
- `?keyword=` 不生效，查重全靠客户端拉全量比对；缺陷量到万级会变慢
- 不支持附件上传（接口不支持）

## License

MIT
