# 禅道 REST API v1 · 实测契约（2026-11-10 验证）

实测环境：`http://<zentao-host>:<port>`（ZenTao 开源版，接口前缀 `/api.php/v1`，主机名已脱敏）。
以下每条都是实测结论，不是文档抄录；「未验证」项已明确标注。

> **换实例必重验**：下文所有「404」「枚举取值」「字段形态」都来自那一次实测。
> 不同版本 / 不同部署方式的禅道，能力可能不一样（尤其 builds、modules、comments 这三个接口）。
> 迁移到新环境时，先跑 `meta --json`，它的 `capabilities` 字段会重新探测一遍，不要照抄本文结论。

## 1. 认证

```http
POST /api.php/v1/tokens
Content-Type: application/json
{"account":"<account>","password":"<password>"}
→ 201 {"token":"<32 位十六进制 token>"}
```

后续所有请求带 Header `Token: <token>`。无 Token → **401 `{"error":"Unauthorized"}`**。
token 有效期以服务端配置为准，脚本每次运行重新登录，不缓存。

> 本文所有 `<...>` 均为占位符，示例值已脱敏。真实账号、口令、token 不进任何文档。

## 2. 缺陷读取

| 用途 | 请求 | 说明 |
|---|---|---|
| 产品缺陷列表 | `GET /products/{id}/bugs?limit=1000` | 返回 `{"page","total","limit","bugs":[...]}` |
| 单条详情 | `GET /bugs/{id}` | 200 返回完整对象 |

**`?keyword=` 不生效**——实测带 `keyword` 参数仍返回该产品全量缺陷，`total` 与不带参数时一致。
**查重必须在客户端做**：脚本拉全量后建三级索引（`[SRC:]` 标记 / 标题原文 / 归一化标题）。

## 3. 缺陷创建（核心）

```http
POST /products/{id}/bugs
Content-Type: application/json
Token: <token>

{
  "title": "[SRC:D-DEMO-01] <一句话现象>",
  "severity": 1,
  "pri": 1,
  "type": "codeerror",
  "module": 0,
  "execution": 0,
  "openedBuild": "1",
  "assignedTo": "<account>",
  "steps": "<p>…HTML…</p>"
}
→ 201 {"id": <新单号>, ...}
```

### 各字段实测行为

| 字段 | 实测结论 |
|---|---|
| `title` | **唯一必填**。空 → `400 {"error":"『Bug标题』不能为空。"}`（校验先于写入，不会产生脏单） |
| `severity` / `pri` | 数字 1–4，原样落库。约定 P0→1、P1→2、P2→3、P3/P4→4 |
| `type` | 字符串枚举。该实例历史缺陷中出现过 4 个取值（`codeerror` / `performance` / `designdefect` / `others`），其中 `codeerror` 占多数。其余官方取值未验证 |
| `openedBuild` | **传字符串 `"1"`**，服务端归一为 `[{"id":"1","title":"<构建名>"}]`；传数字未验证。**注意读回来两种形态**：列表接口是逗号串（多个构建名拼接），详情接口是对象数组 |
| `assignedTo` | 传账号字符串（如 `"<account>"`），正确解析为 `{"account":"<account>",...}`；传不存在的账号行为**未验证**（先拉 `/users` 核对） |
| `steps` | **HTML 原样保留，不做清洗**，可放心塞 `<p><b><a>`。注意：先拼 `<a>` 再整体转义会变成 `&lt;a href="<a href=…` 的乱码 |
| `module` / `execution` | 该实例无对应枚举接口，只能写 0 |

## 4. 缺陷修改

```http
PUT /bugs/{id}
{"steps": "<p>…</p>"}
→ 200 返回更新后的完整对象
```

实测可用，用于回修 `steps`。**该实例没有 `/bugs/{id}/comments` 接口（404）**，所以：
- 报告链接、证据只能写进 `steps`；
- 后续进展追加也只能改 `steps` 正文，不能靠评论追加。

## 5. 该实例缺失的接口（都是 404，规划时别指望）

| 接口 | 结果 | 影响 |
|---|---|---|
| `GET /products/{id}/modules` | 404 | 缺陷无法按模块归类，`module` 只能 0 |
| `GET /products/{id}/builds` | 404 | 构建枚举只能从现有缺陷 `openedBuild` 反推（该产品得到 2 个构建名；其中仅默认构建 id=1 已知） |
| `GET /products/{id}/executions` | 404 | 无法关联执行/迭代，`execution` 只能 0 |
| `GET /projects` | 200 但 `total=0` | 该实例没建项目，`project` 写 0 |
| `GET /bugs/{id}/comments` | 404 | 见上 |
| `/swagger` `/swagger.json` | 200 但返回登录页 | 无 OpenAPI spec 可校验，契约只能靠实测 |

> 404 已用对照端点验证不是「通配 404」：`/products/1/bugsv?limit=1` 同样 404，而 `/products/1/bugs` 200，
> 说明 404 是真实的路由缺失。

## 6. 用户与产品

- `GET /users?limit=100` → 200，`{"page":1,"total":<N>,"limit":100,"users":[{"account":"<account>","realname":"<姓名>"},…]}`
  → 指派前先拉用户列表确认账号存在，别凭猜写 `assignedTo`。
- `GET /products?limit=100` → 200，`{"total":<N>,"products":[{id,name,status,PO,QD,…}]}`
  → 注意 `status`：该实例存在 `closed` 状态的产品，**已关闭的产品不要建单**。
- `GET /products/{id}` → 单个产品（含 PO / QD）。
- 判断「缺陷该进哪个产品」：看该产品下已有缺陷的标题是否与本项目代码/模块对得上，
  或看 `openedBy` 是否为本团队账号。**别新建产品**——建错产品比建错单难清理。
- 写权限的间接证据：`GET /products/{id}/bugs` 后数 `openedBy.account == 当前账号` 的条数。
  `>0` 说明该账号对当前产品有历史提单记录（`auth` 子命令就会打印这个数），比空口断言「有权限」可靠。

## 7. 枚举检索（`meta` 子命令的数据来源）

| 字段 | 来源 | 可信度 |
|---|---|---|
| 产品 | `GET /products` | 直接枚举，可信 |
| 指派人 | `GET /users` + 现有缺陷 `assignedTo` 分布 | 直接枚举，可信 |
| `severity` / `pri` | 现有缺陷取值分布 | **反推**：接口无枚举端点，但 1–4 全档都被用过，取值域确定 |
| `type` | 现有缺陷取值分布 | **反推**：该实例只出现过 4 个取值，官方全量枚举未知 |
| `status` | 现有缺陷取值分布 | **反推**：`active` / `resolved` 等 |
| 构建 `openedBuild` | 现有缺陷 `openedBuild` 反推 | **反推**：`/builds` 404，只能从已用过的构建名聚合；该实例得到 2 个构建名，id 仅默认构建=1 已知 |

> 反推值的可信度低于直接枚举：换实例、或该产品缺陷太少时，反推出的枚举可能不全。
> 所以交互确认时要把「这是从现有缺陷反推的，共 N 个」讲给用户，不能包装成「系统枚举」。

## 8. 排查用语

| 现象 | 大概率原因 |
|---|---|
| `401 Unauthorized` | token 没带上 / 过期，重新 `POST /tokens` |
| `400 标题不能为空` | 端点在，payload 里 `title` 丢了（也是探测端点存活的最好办法） |
| `403` / 空响应 | 账号角色无该产品权限，或产品已 `closed` |
| 步骤里出现 `&lt;a href="<a href=` | 拼 HTML 后又整体 `html.escape`，二次转义 |
| `auth` 返回 `missing_credential` | 本机无凭据文件且无环境变量 → 必须向用户索取，不要翻其他目录的旧文件 |
