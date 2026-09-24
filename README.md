# JSON Schema 兼容性分析 API

用 **FastAPI + SQLite** 构建的 JSON Schema 兼容性分析服务。提交旧版与新版 JSON Schema
（以及可选的样例文档），服务会递归比较两者的**类型、必填字段、枚举、数组、对象、
组合关键字与 if/then/else 条件分支**，
分别判断**向后兼容**与**向前兼容**，并为每一处破坏点生成**最小反例路径、反例文档和原因说明**。

- Web 框架：FastAPI
- 持久化：SQLite + SQLAlchemy
- Schema 校验：jsonschema（自动识别 draft-04 / 06 / 07 / 2019-09 / 2020-12 写法）
- 自动生成 OpenAPI 文档：`/docs`（Swagger UI）

---

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由：创建分析、查询报告、基线对比、发布门禁、SARIF 导出、合规画像、样本回放、回放最小覆盖、变异覆盖评估、x-sensitive 实例脱敏、$ref 引用依赖诊断
│   ├── compat.py        # 兼容性分析引擎（递归比较 + 最小反例生成）
│   ├── refgraph.py      # 单份 schema 的 $ref 引用依赖诊断（引用边 / 不可达 $defs / 循环链 / 无效目标，只读不落库）
│   ├── profile.py       # 合规画像引擎（批量文档校验 + oneOf/anyOf 最深公共失败路径 + 稳定指纹归并）
│   ├── replay.py        # 样本回放诊断（旧/新双版校验 + backward/forward 分类 + 失败归因到报告 issue）
│   ├── reduction.py     # 兼容性反例缩减（固定顺序删除属性/元素/缩短字符串，长度严格变小且重验保持方向）
│   ├── coverage.py      # 回放样本最小覆盖（确定性贪心集合覆盖，复用回放校验与归因选择最少代表文档）
│   ├── mutation.py      # 样本变异覆盖评估（新 schema 单点变异确定性生成 + 逐变异校验 + 分类 mutation score）
│   ├── redact.py        # 基于新 schema x-sensitive 标记的实例脱敏（对象/数组并行遍历 + #/... 本地 $ref + oneOf/anyOf/allOf + 递归引用终止）
│   ├── compare.py       # 报告基线对比（issue 指纹 + 新增/修复/仍存/级别变化分类）
│   ├── sarif.py         # SARIF 2.1.0 导出（issue -> result / rule / logicalLocation）
│   ├── gate.py          # 发布门禁（方向阈值、warning 上限、issue 豁免规则）
│   ├── database.py      # SQLite / SQLAlchemy 初始化
│   ├── models.py        # Analysis ORM 模型
│   └── schemas.py       # Pydantic 请求 / 响应模型
├── tests/
│   ├── test_compat.py   # 引擎单元测试（含 if/then/else 条件分支用例）
│   ├── test_refgraph.py # $ref 引用依赖诊断单元测试（引用边 / 不可达定义 / 循环 / 无效引用 / 确定性）
│   ├── test_profile.py  # 合规画像引擎单元测试（最深公共失败路径 / 指纹 / 顺序无关）
│   ├── test_replay.py   # 样本回放引擎单元测试（四向分类 / issue 归因 / 无法归因归并）
│   ├── test_reduction.py# 反例缩减引擎单元测试（固定顺序三类删除 / 方向保持 / 上限提前结束）
│   ├── test_coverage.py # 回放最小覆盖引擎单元测试（贪心选择 / 并列排序 / 方向筛选 / 未覆盖 issue）
│   ├── test_mutation.py # 变异覆盖引擎单元测试（单点变异生成 / 等价过滤 / 上限 / 杀死判定）
│   ├── test_redact.py   # 实例脱敏引擎单元测试（对象/数组遍历 / $ref / oneOf/anyOf/allOf / 递归终止 / 不可解析引用）
│   ├── test_compare.py  # 基线对比单元测试
│   ├── test_gate.py     # 发布门禁单元测试
│   └── test_api.py      # 接口端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 快速开始（Docker Compose，推荐）

前置条件：已安装 Docker Engine 20.10+ 与 Docker Compose v2
（`docker compose version` 可输出版本号；旧版 Compose v1 请把下文
`docker compose` 替换为 `docker-compose`）。

### 构建并启动

```bash
# 前台启动（构建镜像 + 启动容器，日志直接输出到终端，Ctrl-C 停止）
docker compose up --build

# 或后台启动（守护进程方式）
docker compose up --build -d

# 查看后台运行状态 / 日志
docker compose ps
docker compose logs -f
```

服务启动后监听 **`http://localhost:8000`**：

| 用途          | 地址                                   |
| ------------- | -------------------------------------- |
| API 根路径    | http://localhost:8000/  （跳转 /docs） |
| 健康检查      | http://localhost:8000/health          |
| Swagger 文档  | http://localhost:8000/docs            |
| OpenAPI JSON  | http://localhost:8000/openapi.json    |

容器内置健康检查（每 10s 访问 `/health`，连续 5 次失败标记为
`unhealthy`），可通过 `docker compose ps` 的 `STATUS` 列确认服务就绪。

快速验证：

```bash
curl http://localhost:8000/health
# {"status":"ok"}

# 打开 Swagger UI 可直接调试全部接口（含引用依赖诊断、变异覆盖评估）
# http://localhost:8000/docs
```

不需要先创建分析记录、只想检查一份 schema 的 `$ref` 引用结构时，可直接
调用**引用依赖诊断**接口（只读、不落库；访问地址
`http://localhost:8000/api/reference-diagnostics`）：

```bash
curl -s -X POST http://localhost:8000/api/reference-diagnostics \
  -H "Content-Type: application/json" \
  -d '{
    "schema": {
      "type": "object",
      "properties": {
        "name": {"$ref": "#/$defs/Name"},
        "tree": {"$ref": "#/$defs/Node"},
        "oops": {"$ref": "#/$defs/Missing"}
      },
      "$defs": {
        "Name": {"type": "string"},
        "Node": {
          "type": "object",
          "properties": {"child": {"$ref": "#/$defs/Node"}}
        },
        "Dead": {"type": "integer"}
      }
    }
  }' | python3 -m json.tool
```

启动后可用一条脚本快速走通"创建分析 → 变异覆盖评估"：

```bash
# 1) 创建分析（旧版无约束 -> 新版必填 status/age 且枚举与范围收紧）
ID=$(curl -s -X POST http://localhost:8000/api/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "old_schema": {"type": "object"},
    "new_schema": {
      "type": "object",
      "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
        "age": {"type": "integer", "minimum": 0, "maximum": 100}
      },
      "required": ["status", "age"]
    }
  }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

# 2) 对新 schema 做样本变异覆盖评估
curl -s -X POST "http://localhost:8000/api/analyses/$ID/mutation-coverage" \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"status": "pending", "age": 0},
    {"status": "active", "age": 100},
    {"status": "active"}
  ]}' | python3 -m json.tool
```

也可以一键体验**敏感字段脱敏**：新 schema 中把字段标记
`"x-sensitive": true`，提交文档后只有这些位置被替换为 `"***"`
（访问地址 `http://localhost:8000/api/analyses/<ID>/redact`）：

```bash
# 1) 创建带 x-sensitive 标记的分析
RID=$(curl -s -X POST http://localhost:8000/api/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "old_schema": {"type": "object"},
    "new_schema": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "ssn":  {"type": "string", "x-sensitive": true},
        "cards": {"type": "array", "items": {
          "type": "object",
          "properties": {"number": {"x-sensitive": true}}
        }}
      }
    }
  }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

# 2) 脱敏：ssn 与 cards[].number 被替换为 ***，name 保持原样
curl -s -X POST "http://localhost:8000/api/analyses/$RID/redact" \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"name": "alice", "ssn": "111-22-3333", "cards": [{"number": "4111..."}]}
  ]}' | python3 -m json.tool
```

容器内测试（可选）：仓库自带的接口与引擎测试不会打进镜像；本地源码目录下
可直接运行（需要 Python 3.11+ 与开发依赖）：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

### 常见启动问题

- **端口被占用**：宿主机 8000 端口已被其他进程使用时，修改
  `docker-compose.yml` 中 `"8000:8000"` 的左侧端口（如 `"18000:8000"`），
  或 `docker run` 时改用 `-p 18000:8000`；
- **容器一直 unhealthy / 立即退出**：`docker compose logs api` 查看日志；
  常见原因是数据卷权限或旧版数据库文件与当前 SQLAlchemy 模型不匹配
  （开发环境可用 `docker compose down -v` 清卷重建，**会清空历史数据**）；
- **镜像内代码未更新**：改了 `app/` 代码后需 `docker compose up --build`
  重新构建（镜像只 COPY `app` 目录，不挂载源码卷）。

### 数据持久化

SQLite 数据文件位于容器内 `/app/data/schema_compat.db`，通过命名卷
`schema_compat_data` 持久化，容器重建（`up --build`）后历史报告与画像
仍在。Docker 会给卷名加上 Compose 项目名前缀（默认是目录名，实际卷名
形如 `schema-compat_schema_compat_data`）：

```bash
# 查看实际卷名与挂载位置
docker volume ls | grep schema_compat
docker volume inspect "$(docker volume ls -q | grep schema_compat)"

# 停止容器（保留数据卷）
docker compose down

# 停止并删除数据卷（⚠️ 清空全部分析 / 画像历史）
docker compose down -v
```

### 不使用 Compose，直接用 Docker

```bash
# 构建镜像
docker build -t schema-compat-api .

# 运行容器（8000 端口映射 + 数据卷持久化）
docker run -d --name schema-compat-api \
  -p 8000:8000 \
  -v schema_compat_data:/app/data \
  schema-compat-api

# 查看日志 / 停止
docker logs -f schema-compat-api
docker stop schema-compat-api && docker rm schema-compat-api
```

---

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可通过环境变量覆盖数据库位置：

```bash
DATABASE_URL="sqlite:///./data/schema_compat.db" uvicorn app.main:app
```

运行测试：

```bash
pytest -q
```

---

## 兼容性判定模型

设 `Old(S)` 表示旧 schema 接受的全部实例，`New(S)` 表示新 schema 接受的全部实例。

| 方向 | 定义 | 含义 | 反例 |
| ---- | ---- | ---- | ---- |
| **向后兼容** `backward` | `New ⊇ Old` | 新版能读取所有旧数据 | 旧版合法、**新版拒绝**的文档 |
| **向前兼容** `forward` | `Old ⊇ New` | 旧版能读取所有新数据 | 新版合法、**旧版拒绝**的文档 |

典型经验：

- 新增**可选**字段：两个方向都兼容；
- 新增**必填**字段：破坏向后兼容；
- **删除**必填字段：破坏向前兼容；
- 收窄枚举 / 收紧数值范围 / 提高 minItems：破坏向后兼容；
- 类型直接改变（string ↔ integer）：两个方向都破坏。

### 检查覆盖的关键字

- `type`（含 integer/number 关系、无 type 时的结构推断）
- `required`、`properties`、`additionalProperties`、`patternProperties`、
  `minProperties` / `maxProperties`
- `enum`、`const`
- `items`（齐次数组）、`prefixItems` / draft-04 列表形式 `items`（元组）、
  `additionalItems`、`minItems` / `maxItems`、`uniqueItems`
- `minimum` / `maximum`、draft-04 布尔型 `exclusiveMinimum/Maximum`、
  `exclusiveMinimum/Maximum` 数值写法、`multipleOf`
- `minLength` / `maxLength`、`pattern`
- 组合关键字：`allOf`（浅合并后比较）、`anyOf`、`oneOf`、`not`
- 条件关键字：`if` / `then` / `else`（draft-07+，按实例命中情况分
  激活单元比较，详见下文）
- 文档内部 `$ref`（`#/$defs/...`，带环保护）

### 条件分支（if / then / else，draft-07+）

实例按是否命中 `if` 被路由到 `then` 或 `else`，因此引擎把每个条件节点
拆成"**激活单元**"（源命中 / 未命中 × 目标命中 / 未命中）分别递归比较，
属性内嵌套的更深层条件同样逐层处理：

- 命中走 `then`、未命中走 `else`：同分支单元直接比较分支内容；
- 新旧 `if` 条件不一致时，额外检查"源命中、目标未命中"（以及相反）的
  跨分支路由单元，并在条件节点上按需**强制构造满足 / 违反 `if` 的实例**
  （鉴别器属性合并、节点枚举内取值替换、required 属性移除等）来激活
  对应分支；
- 仅一侧有条件时，另一侧整体参与比较。

判定与反例规则：

- 只有构造出的文档**经验证满足源 schema 且被目标 schema 拒绝**时才记
  `error` 并附最小反例；无法自动验证时降级为 `warning`；
- **未激活的分支不会单独被判为破坏**（如 `if` 永假时 `then` 的差异只
  产生 warning）；
- `issue.path` 定位到发生变化的条件或分支：`#/if`（条件表达式变化，
  code `if_condition_changed`）、`#/then/...`、`#/else/...`（分支内部
  变化，沿用 `required_added`、`enum_value_removed` 等既有 code）。

### 反例的可信度

每一个反例在返回前都会经过真实的 `jsonschema` 校验，确保**满足 source schema
且确实被 target schema 拒绝**，不会返回想当然的假反例。

- `severity: error`：找到了经过验证的最小反例，确定破坏兼容；
- `severity: warning`：启发式规则（如 `pattern` 变更、`multipleOf` 新增、
  `anyOf` 新增）或条件分支中无法自动构造出经过验证的反例，标记为潜在
  风险供人工确认。

`path` 使用 JSON Pointer 风格定位到 schema 中出问题的关键字位置（如
`#/properties/addresses/items`）；`counterexample_path` 定位反例文档中
触发问题的值位置（如 `#/addresses/0`）。

---

## API

### `POST /api/analyses` — 创建兼容性分析

请求体：

| 字段        | 类型    | 必填 | 说明                       |
| ----------- | ------- | ---- | -------------------------- |
| `name`      | string  | 否   | 本次分析的名称             |
| `old_schema`| object  | 是   | 旧版 JSON Schema           |
| `new_schema`| object  | 是   | 新版 JSON Schema           |
| `samples`   | array   | 否   | 真实样例文档，用于精准定位 |

```bash
curl -X POST http://localhost:8000/api/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "name": "user v1 -> v2",
    "old_schema": {
      "type": "object",
      "properties": {
        "id":   {"type": "integer"},
        "role": {"type": "string", "enum": ["admin", "editor", "viewer"]}
      },
      "required": ["id", "role"]
    },
    "new_schema": {
      "type": "object",
      "properties": {
        "id":    {"type": "string"},
        "role":  {"type": "string", "enum": ["admin", "editor"]},
        "email": {"type": "string"}
      },
      "required": ["id", "role", "email"]
    },
    "samples": [
      {"id": 42, "role": "viewer"}
    ]
  }'
```

响应（节选）：

```json
{
  "id": 1,
  "name": "user v1 -> v2",
  "created_at": "2026-09-21T15:00:00+00:00",
  "report": {
    "backward_compatible": false,
    "forward_compatible": false,
    "summary": {
      "backward": { "errors": 4, "warnings": 0, "total": 4 },
      "forward":  { "errors": 1, "warnings": 0, "total": 1 },
      "total": 5, "errors": 5, "warnings": 0
    },
    "issues": [
      {
        "direction": "backward",
        "path": "#/properties/role",
        "code": "enum_value_removed",
        "severity": "error",
        "message": "枚举值 \"viewer\" 在新版中已被移除",
        "reason": "旧版枚举包含 \"viewer\"，新版枚举不再包含该值，使用该值的旧数据会校验失败",
        "counterexample": { "id": 0, "role": "viewer" },
        "counterexample_path": "#/role"
      },
      {
        "direction": "backward",
        "path": "#/properties/id",
        "code": "type_removed",
        "severity": "error",
        "message": "新版不再接受类型 integer",
        "reason": "旧版允许类型 integer，新版仅允许 ['string']，该类型的旧数据会校验失败",
        "counterexample": { "id": 0, "role": "admin" },
        "counterexample_path": "#/id"
      }
    ],
    "sample_results": [
      { "index": 0, "valid_against_old": true, "valid_against_new": false,
        "direction": "backward" }
    ]
  }
}
```

提交的 schema 本身不合法时返回 `400`：

```json
{ "detail": { "field": "old_schema", "message": "非法 JSON Schema: ..." } }
```

### `GET /api/analyses` — 列出分析（摘要）

支持 `limit`（默认 50，最大 200）和 `offset` 分页：

```bash
curl "http://localhost:8000/api/analyses?limit=20"
```

```json
{
  "total": 1,
  "items": [
    {
      "id": 1,
      "name": "user v1 -> v2",
      "created_at": "2026-09-21T15:00:00+00:00",
      "backward_compatible": false,
      "forward_compatible": false,
      "errors": 5,
      "warnings": 0
    }
  ]
}
```

### `GET /api/analyses/{id}` — 获取完整报告

返回与 `POST` 完全相同的完整结构（含两个 schema、样例结果与全部 issues）：

```bash
curl http://localhost:8000/api/analyses/1
```

记录不存在返回 `404`。

### `GET /api/analyses/{id}/sarif` — 导出 SARIF 2.1.0 报告

将一份已落库的分析报告转换为 **SARIF v2.1.0**（Static Analysis Results
Interchange Format，`Content-Type: application/sarif+json`），可直接对接
GitHub code scanning 等消费 SARIF 的工具链。接口为**纯只读**操作，不会
修改原始报告；分析编号不存在返回 `404`。

```bash
curl http://localhost:8000/api/analyses/1/sarif
```

映射规则：

- 每条 issue 转为一个 **result**，完全重复的 issue 也**分别保留**，不做合并；
- **rule 按 `code` 去重生成**（同一 code 仅一条，按 ruleId 字典序排列）；
  rule 的 `defaultConfiguration.level` 取该 code 下出现过的最高严重级别
  （error > warning），已知 code 附 `shortDescription` 中文说明；
- issue 的 `severity` 映射为 result 的 SARIF **`level`**
  （`error` → `error`，`warning` → `warning`）；
- `direction`（兼容方向，非 SARIF 标准字段）放入 result 的
  **`properties.direction`**；
- schema `path` 作为**逻辑位置**：去重后登记到
  `run.logicalLocations`（含 `name` 与 JSON Pointer 风格的
  `fullyQualifiedName`），result 经 `locations.logicalLocations.index`
  引用；
- 每个 result 带**稳定 `partialFingerprints.businessIdentity/v1`**：
  由 direction + path + code + 规范化 reason 的 SHA-256（前 16 位）生成，
  **不含 message**，同一业务问题仅措辞 / 严重级别变化时指纹保持不变；
- `results` 按 (direction, ruleId, path, level, 指纹) **稳定键排序**；
  键完全相同的重复项保持报告内相对顺序，因此重复 issue 不会被去重。

响应（节选）：

```json
{
  "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
  "version": "2.1.0",
  "runs": [
    {
      "tool": {
        "driver": {
          "name": "json-schema-compat-analyzer",
          "version": "1.0.0",
          "semanticVersion": "1.0.0",
          "rules": [
            {
              "id": "enum_value_removed",
              "name": "enum_value_removed",
              "defaultConfiguration": { "level": "error" },
              "shortDescription": { "text": "枚举值在新版中被删除" }
            },
            {
              "id": "required_added",
              "name": "required_added",
              "defaultConfiguration": { "level": "error" },
              "shortDescription": { "text": "字段在新版中变为必填" }
            }
          ]
        }
      },
      "logicalLocations": [
        { "name": "#", "fullyQualifiedName": "#" },
        { "name": "status", "fullyQualifiedName": "#/properties/status" }
      ],
      "results": [
        {
          "ruleId": "enum_value_removed",
          "level": "error",
          "message": { "text": "枚举值 \"pending\" 在新版中已被移除" },
          "locations": [
            { "logicalLocations": [
              { "fullyQualifiedName": "#/properties/status", "index": 1 }
            ] }
          ],
          "partialFingerprints": { "businessIdentity/v1": "65d42cb616ce1b35" },
          "properties": { "direction": "backward" }
        }
      ]
    }
  ]
}
```

### `POST /api/analyses/compare` — 基线对比两次分析报告

接收 `baseline_id` 与 `current_id`，对两份已落库的报告做差异分析。
接口为**纯只读**操作，不会修改任何原始报告。

请求体：

| 字段          | 类型 | 必填 | 说明           |
| ------------- | ---- | ---- | -------------- |
| `baseline_id` | int  | 是   | 基线分析记录 ID |
| `current_id`  | int  | 是   | 当前分析记录 ID |

```bash
curl -X POST http://localhost:8000/api/analyses/compare \
  -H "Content-Type: application/json" \
  -d '{"baseline_id": 1, "current_id": 2}'
```

响应（节选）：

```json
{
  "baseline_id": 1,
  "current_id": 2,
  "compatibility": {
    "backward": { "baseline": false, "current": true, "changed": true },
    "forward":  { "baseline": true,  "current": true, "changed": false }
  },
  "summary_diff": {
    "errors":   { "baseline": 4, "current": 1, "delta": -3 },
    "warnings": { "baseline": 0, "current": 1, "delta": 1 },
    "total":    { "baseline": 4, "current": 2, "delta": -2 },
    "backward": { "errors": { "baseline": 3, "current": 0, "delta": -3 },
                  "warnings": { "baseline": 0, "current": 1, "delta": 1 },
                  "total":    { "baseline": 3, "current": 1, "delta": -2 } },
    "forward":  { "errors": { "baseline": 1, "current": 1, "delta": 0 },
                  "warnings": { "baseline": 0, "current": 0, "delta": 0 },
                  "total":    { "baseline": 1, "current": 1, "delta": 0 } }
  },
  "issues": {
    "added":            [ { "fingerprint": "9f2c…", "direction": "backward",
                            "path": "#", "code": "required_added", … } ],
    "fixed":            [ … ],
    "persisting":       [ … ],
    "severity_changed": [ { "fingerprint": "51ab…", "code": "integer_required",
                            "baseline_severity": "error",
                            "current_severity": "warning", … } ]
  },
  "counts": { "added": 1, "fixed": 2, "persisting": 1, "severity_changed": 1 }
}
```

任一分析编号不存在时返回 `404`：

```json
{ "detail": "基线分析记录不存在: 999999" }
```

#### issue 指纹与分类规则

每条 issue 的**稳定指纹**由以下字段生成：方向（`direction`）、schema 路径
（`path`）、问题代码（`code`）以及**规范化后的原因**（`reason`）。规范化会：

- 剥离启发式降级后缀（引擎将 error 降级为 warning 时追加的说明文字）——
  这样同一问题仅严重级别变化时指纹保持不变；
- 压缩连续空白、去除首尾空白，使指纹对格式抖动免疫。

指纹为 SHA-256 前 16 个十六进制字符，**不包含 severity**。按指纹对两份报告
做多重集配对后，每条问题被分类为：

| 分类              | 含义                                       |
| ----------------- | ------------------------------------------ |
| `added`           | 仅出现在当前报告 —— 新增问题               |
| `fixed`           | 仅出现在基线报告 —— 已修复问题             |
| `persisting`      | 两份报告都存在且严重级别相同 —— 仍存在     |
| `severity_changed`| 两份报告都存在但严重级别不同 —— 仅级别变化 |

`compatibility` 给出两个兼容方向（backward / forward）在两次分析间的变化，
`summary_diff` 给出错误 / 警告 / 总数（含分方向）的基线值、当前值与差值。

### `POST /api/analyses/{id}/gate` — 发布门禁

对一份已落库的分析报告执行发布门禁评估。接口为**纯只读**操作，不会修改
原始报告；分析编号不存在返回 `404`。

请求体：

| 字段           | 类型     | 必填 | 说明                                            |
| -------------- | -------- | ---- | ----------------------------------------------- |
| `directions`   | string[] | 是   | 需要检查的兼容方向（至少一个，重复值自动去重）  |
| `max_warnings` | object   | 否   | 各方向允许的未豁免 warning 上限，缺省视为 `0`   |
| `exemptions`   | object[] | 否   | issue 豁免规则，按声明顺序确定优先级            |

`exemptions` 规则的三个字段均必填：

| 字段        | 说明                                                                 |
| ----------- | -------------------------------------------------------------------- |
| `direction` | 兼容方向 `backward` / `forward`，必须**字面相等**                    |
| `code`      | issue 代码，必须**字面相等**                                         |
| `path`      | JSON Pointer 风格路径（`#`、`#/properties/a`），支持**单段** `*` 通配 |

`path` 通配规则：

- `*` 必须独占一整段：`#/properties/*` 可匹配 `#/properties/a`，
  但不匹配 `#/properties/a/items`；`#/properties/a*` 中的 `*` 是普通字符；
- 路径与报告 issue 的 `path` 字段同样遵守 JSON Pointer 转义（`~1`、`~0`）；
- 段数必须一致，`#/*` 不匹配根路径 `#`。

**规则去重（同一 issue 命中多条规则时）**，依次按以下条件选出唯一规则：

1. **精确字段数多者优先**：path 不含 `*` 时 direction / code / path 三字段
   全部精确（3）；path 含通配段时仅前两者精确（2）；
2. **path 字面段数多者优先**：通配段不计入字面段，`#` 为 0 段
   （如 `#/properties/*/items` 字面段数为 2，`#/*/*/items` 为 1）；
3. **声明顺序靠后者优先**：以上都相同时，以 `exemptions` 数组中**下标更大**
   （声明更晚）的规则为准。

**判定规则**：

- 被豁免的 issue **不计数**，也不会成为阻断项；豁免项会返回其命中的规则
  下标 `rule_index`（`exemptions` 数组从 **0** 开始）；
- 任一被检查方向存在**未豁免 error**，或**未豁免 warning 数超过该方向
  `max_warnings` 上限**，门禁即失败（`pass: false`）；
- 超限 warning 按报告顺序（direction → path → code）计数，排在额度之外的
  成为阻断项；
- `directions` 未包含的方向即使存在 error / warning 也**不影响结论**。

```bash
curl -X POST http://localhost:8000/api/analyses/1/gate \
  -H "Content-Type: application/json" \
  -d '{
    "directions": ["backward"],
    "max_warnings": {"backward": 1},
    "exemptions": [
      {"direction": "backward", "code": "required_added", "path": "#"},
      {"direction": "backward", "code": "pattern_changed",
       "path": "#/properties/*"}
    ]
  }'
```

响应：

```json
{
  "pass": true,
  "counts": {
    "backward": {
      "errors": 0,
      "warnings": 0,
      "exempted": 3,
      "total": 3,
      "max_warnings": 1
    }
  },
  "blocking": [],
  "exempted": [
    {
      "direction": "backward",
      "path": "#",
      "code": "required_added",
      "severity": "error",
      "message": "字段 'b' 在新版中变为必填",
      "reason": "旧版中 'b' 可以缺省……",
      "counterexample": {},
      "counterexample_path": "#",
      "rule_index": 0
    },
    {
      "direction": "backward",
      "path": "#/properties/a",
      "code": "pattern_changed",
      "severity": "warning",
      "message": "新版正则约束为 '^xx'",
      "reason": "旧版 pattern 为 '^x'……",
      "counterexample": null,
      "counterexample_path": null,
      "rule_index": 1
    }
  ]
}
```

失败时 `blocking` 中每条阻断项在 issue 原有字段之外附带 `blocked_reason`：

| `blocked_reason`            | 含义                       |
| --------------------------- | -------------------------- |
| `error`                     | 未豁免的 error             |
| `warning_limit_exceeded`    | 未豁免 warning 数超出上限   |

请求体不合法（如 `directions` 为空、规则 `path` 不是合法 JSON Pointer、
上限为负数）返回 `422`。

### `POST /api/analyses/{id}/profile` — 新 schema 批量文档合规画像

用分析记录保存的**新 schema**（`new_schema`）对最多 **5000** 个待检 JSON
文档逐个校验，返回合规率与按 JSON Pointer 归并的失败计数 / 示例下标，
并把**画像快照关联分析记录持久化**（同一分析下版本号从 1 递增）。
画像为追加写入，绝不修改原分析记录；分析编号不存在返回 `404`。

请求体：

| 字段        | 类型     | 必填 | 说明                                        |
| ----------- | -------- | ---- | ------------------------------------------- |
| `documents` | object[] | 是   | 待检 JSON 文档（必须全部为对象），数量 1..5000 |

**入参校验（返回 `422` 且不写入任何画像记录）**：

- 文档数量超过 5000 或为空列表；
- 任一文档不是 JSON 对象（数字 / 字符串 / 布尔 / `null` / 数组均拒绝，
  即使与合法对象混在同一批中也整批拒绝）。

```bash
curl -X POST http://localhost:8000/api/analyses/1/profile \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"name": "ok", "age": 3},
    {"age": -1},
    {"name": 5, "age": 1}
  ]}'
```

响应：

```json
{
  "id": 1,
  "analysis_id": 1,
  "version": 1,
  "created_at": "2026-09-22T10:00:00+00:00",
  "total_documents": 3,
  "valid_documents": 1,
  "invalid_documents": 2,
  "compliance_rate": 0.3333333333333333,
  "failures": [
    {
      "fingerprint": "36272e41e33a701e",
      "pointer": "#/age",
      "validator": "minimum",
      "schema_pointer": "#/properties/age/minimum",
      "message": "数值必须大于等于 0",
      "count": 1,
      "example_indices": [1]
    },
    {
      "fingerprint": "7342d4b7bd2e70e7",
      "pointer": "#/name",
      "validator": "type",
      "schema_pointer": "#/properties/name/type",
      "message": "值的类型必须为 string",
      "count": 1,
      "example_indices": [2]
    }
  ]
}
```

响应字段：

| 字段               | 说明                                                           |
| ------------------ | -------------------------------------------------------------- |
| `compliance_rate`  | 合规率 = `valid_documents / total_documents`                   |
| `failures[].pointer` | 失败位置（JSON Pointer，根为 `#`），oneOf/anyOf 为最深公共失败路径 |
| `failures[].validator` | jsonschema 规则名（`required` / `type` / `oneOf` …）          |
| `failures[].schema_pointer` | schema 中触发规则的位置（仅排查用，不参与指纹）         |
| `failures[].fingerprint` | 稳定规则指纹（SHA-256 前 16 位），同类问题合并到同一组      |
| `failures[].count` | 命中该问题的文档数（每个文档在同一组内最多计一次）             |
| `failures[].example_indices` | 示例文档下标（基于提交顺序，从 0 开始，升序且有界） |

#### oneOf / anyOf 嵌套错误与稳定指纹

`oneOf` / `anyOf` 失败时 jsonschema 返回的是一棵分支错误树。画像引擎会
**递归收集全部叶子失败**，取叶子实例路径的**最长公共前缀**作为
`pointer`：

- 所有分支都在同一深层位置失败（如都失败在 `#/kind/v`）→ 定位到该
  深层位置；
- 分支在不同位置失败（`#/a` 与 `#/b`）→ 回退到公共祖先（可能为根 `#`）。

若所有叶子的规则与规则主体一致（如三个分支的 `#/a` 都是 `type`
失败），用叶子规则（`type`）代表整棵子树；否则以组合关键字
（`oneOf` / `anyOf`）作为规则。

**稳定规则指纹**的输入为：失败位置（pointer）+ 规则（validator）+
规则主体（subject）。规则主体只保留与 schema 声明绑定的信息
（`required` 取缺失字段名、`additionalProperties` 取未允许的键名，
其余规则为空），从而剔除实例取值、数组长度、枚举实际值、命中分支
下标等数据噪声——不同文档违反同一条 schema 规则时合并到同一组。
指纹为 SHA-256 前 16 位（字段间以 `\x1f` 分隔）。

**输入顺序不影响分组结果**：分组按 (pointer, validator,
schema_pointer, 指纹) 稳定排序；`example_indices` 始终以提交下标升序
表示，因此打乱文档顺序只会改变示例下标本身，分组、计数与消息不变。
失败消息只描述规则（如"数值必须大于等于 0"），不含实例实际取值。

#### 画像快照查询接口

| 方法 & 路径 | 说明 |
| ----------- | ---- |
| `GET /api/analyses/{id}/profile` | 获取该分析的**最新**画像快照（无快照时 `404`） |
| `GET /api/analyses/{id}/profiles` | 列出该分析的全部画像快照（摘要，版本倒序） |
| `GET /api/analyses/{id}/profiles/{profile_id}` | 按画像 ID 获取指定快照（不属于该分析时 `404`） |

```bash
curl http://localhost:8000/api/analyses/1/profiles
```

```json
{
  "analysis_id": 1,
  "total": 2,
  "items": [
    {
      "id": 2, "version": 2,
      "created_at": "2026-09-22T11:00:00+00:00",
      "total_documents": 100, "valid_documents": 91, "invalid_documents": 9,
      "compliance_rate": 0.91, "failure_groups": 4
    }
  ]
}
```

### `POST /api/analyses/{id}/replay` — 样本回放诊断

对一批**分析样本之外**的真实文档（最多 **5000** 个，支持对象 / 数组 /
数字 / 字符串 / `null` 等任意 JSON 类型），分别用分析记录保存的**旧版
与新版 schema** 逐个校验，按结果把文档分成互斥的四类，并把单向失败的
校验错误**归因到该分析报告中已有的 issue**（按 `direction + path + code`
匹配）。接口为**纯只读**操作：不修改分析报告、不追加画像快照、不写任何
历史；分析编号不存在返回 `404`。

请求体：

| 字段        | 类型    | 必填 | 说明                                          |
| ----------- | ------- | ---- | --------------------------------------------- |
| `documents` | any[]   | 是   | 待回放的 JSON 文档列表，数量 1..5000（任意类型） |

```bash
curl -X POST http://localhost:8000/api/analyses/1/replay \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"status": "pending"},
    {"status": "active"},
    {"status": "active", "owner": "alice"},
    42
  ]}'
```

**文档分类**（`counts`，四类互斥且总和等于文档总数）：

| 分类            | 判定条件                    | 含义                                   |
| --------------- | --------------------------- | -------------------------------------- |
| `backward`      | 旧版合法、**新版拒绝**      | 命中向后不兼容的实锤样本               |
| `forward`       | 新版合法、**旧版拒绝**      | 命中向前不兼容的实锤样本               |
| `valid`         | 两版均合法                  | 与本次 schema 演进无关的正常数据       |
| `invalid_both`  | **两版均拒绝**              | 无法归因于演进：脏数据或两版共有约束   |

响应：

```json
{
  "analysis_id": 1,
  "total_documents": 4,
  "counts": { "valid": 1, "backward": 2, "forward": 0, "invalid_both": 1 },
  "issue_hits": [
    {
      "direction": "backward",
      "path": "#",
      "code": "required_added",
      "severity": "error",
      "count": 2,
      "example_indices": [0, 1]
    },
    {
      "direction": "backward",
      "path": "#/properties/status",
      "code": "enum_value_removed",
      "severity": "error",
      "count": 1,
      "example_indices": [0]
    }
  ],
  "unattributed": [
    {
      "direction": "both",
      "reason": "rejected_by_both",
      "pointer": "#",
      "validator": "type",
      "schema_pointer": "#/type",
      "message": "值的类型必须为 object",
      "count": 1,
      "example_indices": [3]
    }
  ]
}
```

响应字段：

| 字段                            | 说明                                                         |
| ------------------------------- | ------------------------------------------------------------ |
| `issue_hits[]`                  | 报告中**每一条** issue 的命中情况，顺序与报告 issues 一致；未命中 `count=0` |
| `issue_hits[].count`            | 命中该 issue 的文档数（每文档对同一 issue 最多计一次，一篇文档可同时命中多条 issue） |
| `issue_hits[].example_indices`  | 示例文档下标（基于提交顺序，从 0 开始，升序，最多保留 20 个） |
| `unattributed[]`                | **无法归因**到报告 issue 的校验错误分组                      |
| `unattributed[].reason`         | `no_matching_issue`（单向失败但报告无对应 issue）或 `rejected_by_both`（两版都拒绝） |
| `unattributed[].direction`      | `backward` / `forward` / `both`                              |
| `unattributed[].pointer`        | 失败所在的 schema 节点位置（JSON Pointer，根为 `#`）         |
| `unattributed[].validator`      | jsonschema 规则名（`required` / `type` / `minimum` / `oneOf` …）或 `false` |
| `unattributed[].count`          | 命中该错误的文档数；`example_indices` 同为有界升序下标       |

#### 归因规则

- 值约束类错误（`type` / `minimum` / `enum` / `minItems` …）映射到
  触发关键字所属的 **schema 节点**路径（去掉 `absolute_schema_path`
  末段关键字），再映射到对应 issue code（如 `minimum` →
  `minimum_increased`，`enum` → `enum_value_removed` / `enum_added`，
  同一规则的多个候选 code 按报告中实际存在者命中）；
- `required` / `additionalProperties` 的关键字写在容器节点上，直接定位到
  容器（如根级必填缺失 → `#`）；同一位置同一 code 因**规则主体**不同
  （如同时新增两个根级必填字段）而存在多条 issue 时，按缺失字段名 /
  额外键名区分归因；
- `oneOf` / `anyOf` 的嵌套错误树递归展开到叶子；分析器只在组合节点上报
  分支不兼容，因此分支内的任何叶子失败都归因到组合节点的
  `oneOf/anyOf_branch_missing / _branch_rejected / _added` issue；
- 子 schema 为 `false` 时 jsonschema 的错误不带路径，引擎在 `$ref`
  预解析后的 schema 与文档上并行下钻定位真正声明 false 的节点
  （归因到 `schema_forbids_all`）；
- 创建分析时随 `samples` 产生的 `sample_rejected_by_new/old` issue 使用
  **实例位置**（如 `#/arr`），仅在失败实例位置精确一致时作为兜底归因；
- 归因不到任何报告 issue 的单向失败（分析器未覆盖的关键字、启发式未
  落成结构 issue 等）进入 `unattributed`（`no_matching_issue`），不做
  跨节点的模糊回退，避免错误归因。

入参为空列表、超过 5000 个文档或缺少 `documents` 字段时返回 `422`。

### `POST /api/analyses/{id}/replay-coverage` — 回放样本最小覆盖

在[样本回放诊断](#post-apianalysesidreplay--样本回放诊断)的**旧 / 新双版
校验与 issue 归因**之上，解决"从一大批候选文档中挑出**尽可能少的代表
文档**，让报告中每条可被样本触发的 issue 至少被一篇代表文档命中"的问题
（集合覆盖）。接口为**纯只读**操作：不修改分析报告、不追加画像快照、
不写任何历史；分析编号不存在返回 `404`。

请求体：

| 字段          | 类型     | 必填 | 说明                                                 |
| ------------- | -------- | ---- | ---------------------------------------------------- |
| `documents`   | any[]    | 是   | 候选回放 JSON 文档列表（任意类型），数量 1..5000     |
| `directions`  | string[] | 是   | 参与覆盖的方向筛选（至少一个，`backward` / `forward`，重复值自动去重） |

**确定性贪心集合覆盖**：每篇候选文档经双版校验后得到它能覆盖的报告
issue 集合（身份为 `direction + path + code + 规则主体`，与回放归因一致），
随后逐轮选择代表文档直到没有文档能再带来新增覆盖。每轮的排序键为：

1. **新增覆盖数**（相对已覆盖集合的边际收益）大者优先；
2. 新增覆盖数相同时，**文档索引**（提交顺序下标）小者优先。

只覆盖零边际收益 issue 的文档（`valid` / 两版都拒绝 / 仅命中已覆盖
issue）不会被选中；候选文档都无法命中的 issue 进入 `uncovered_issues`。

```bash
curl -X POST http://localhost:8000/api/analyses/1/replay-coverage \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"status": "active", "owner": "alice"},
    {"status": "pending"},
    {"status": "active"},
    42
  ], "directions": ["backward"]}'
```

响应：

```json
{
  "analysis_id": 1,
  "total_documents": 4,
  "directions": ["backward"],
  "counts": { "valid": 1, "backward": 2, "forward": 0, "invalid_both": 1 },
  "total_issues": 3,
  "covered_issue_count": 2,
  "uncovered_issue_count": 1,
  "selected_indices": [1],
  "selected_documents": [
    {
      "index": 1,
      "classification": "backward",
      "newly_covered_issues": 2,
      "covered_issues": 2
    }
  ],
  "covered_issues": [
    {
      "direction": "backward",
      "path": "#",
      "code": "required_added",
      "severity": "error",
      "reason": "旧版中 'owner' 可以缺省，新版 required 包含该字段……",
      "covered_by": [1]
    },
    {
      "direction": "backward",
      "path": "#/properties/status",
      "code": "enum_value_removed",
      "severity": "error",
      "reason": "旧版枚举包含 \"pending\"，新版枚举不再包含该值……",
      "covered_by": [1]
    }
  ],
  "uncovered_issues": [
    {
      "direction": "backward",
      "path": "#",
      "code": "required_added",
      "severity": "error",
      "reason": "旧版中 'status' 可以缺省，新版 required 包含该字段……"
    }
  ]
}
```

响应字段：

| 字段                              | 说明                                                            |
| --------------------------------- | --------------------------------------------------------------- |
| `directions`                      | 实际生效的方向筛选（去重后按 backward → forward 固定顺序）      |
| `counts`                          | **全部候选文档**的原始四向分类计数（不做方向筛选，与回放接口一致） |
| `total_issues`                    | 筛选方向内报告 issue 总数（按业务身份去重）                     |
| `selected_indices`                | 贪心选中的代表文档下标（按选中先后；并列时新增覆盖数大、索引小者优先） |
| `selected_documents[].classification` | 代表文档的原始四向分类                                     |
| `selected_documents[].newly_covered_issues` | 该文档在被选中轮次新增覆盖的 issue 数（必定 ≥ 1）         |
| `selected_documents[].covered_issues` | 该文档可命中的筛选方向内 issue 总数（含与前序代表重复覆盖部分） |
| `covered_issues[]`                | 被覆盖的 issue（按报告内顺序），`covered_by` 给出覆盖它的代表下标（升序） |
| `uncovered_issues[]`              | 候选文档均无法覆盖的 issue（按报告内顺序）                      |

入参为空列表、超过 5000 个文档、缺少 `documents` / `directions` 字段、
`directions` 为空或含非法方向时返回 `422`。

### `POST /api/analyses/{id}/mutation-coverage` — 样本变异覆盖评估

针对分析记录保存的**新 schema**（`new_schema`）**确定性地生成一批单点
变异体（mutants）**，再用提交的待评估文档逐个校验：同一文档在原 schema
与变异体上的校验结果不同，即该变异体被样本"杀死"。接口为**纯只读**
操作：不修改分析报告、不追加画像快照、不写任何历史；分析编号不存在
返回 `404`。

请求体：

| 字段            | 类型     | 必填 | 说明                                                |
| --------------- | -------- | ---- | --------------------------------------------------- |
| `documents`     | any[]    | 是   | 待评估 JSON 文档列表（任意类型），数量 1..5000      |
| `max_per_rule`  | object   | 否   | 每类变异的生成上限；缺省每类 50，传 `0` 关闭该类变异 |

生成的变异规则（每个变异体只改动 schema 的**一处**约束）：

| 规则（rule）                  | 变异内容                       | 方向   |
| ----------------------------- | ------------------------------ | ------ |
| `required_removed`            | 删除一个必填字段               | 放松   |
| `enum_narrowed`               | 枚举删除一个取值               | 收紧   |
| `minimum_increased`           | 数值下界提高（含排他边界）     | 收紧   |
| `maximum_decreased`           | 数值上界降低（含排他边界）     | 收紧   |
| `type_narrowed`               | 多类型声明删除一个类型         | 收紧   |
| `min_length_increased`        | 字符串最短长度 +1              | 收紧   |
| `max_length_decreased`        | 字符串最长长度 -1              | 收紧   |
| `min_items_increased`         | 数组最少元素数 +1              | 收紧   |
| `max_items_decreased`         | 数组最多元素数 -1              | 收紧   |
| `min_properties_increased`    | 对象最少属性数 +1              | 收紧   |
| `max_properties_decreased`    | 对象最多属性数 -1              | 收紧   |

变异在 `$ref` 预解析后的 schema 上生成，覆盖对象属性、齐次/元组数组、
`anyOf` / `oneOf` / `allOf` 分支与 `then` / `else` 子 schema；不改变
`not` / `if` 内层约束（方向会反转）与未内联的 `$defs` 副本。**等价
变异**（删除后不改变校验语义，如 `["integer","number"]` 删除
`integer`）会在变异节点局部用区分见证双向校验后剔除，不计入分母。

```bash
curl -X POST http://localhost:8000/api/analyses/1/mutation-coverage \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {"status": "pending", "age": 0},
      {"status": "active", "age": 100},
      {"status": "active"}
    ],
    "max_per_rule": {"enum_narrowed": 10}
  }'
```

响应（节选）：

```json
{
  "analysis_id": 1,
  "total_documents": 3,
  "valid_documents": 2,
  "invalid_documents": 1,
  "total_mutations": 8,
  "killed_mutations": 6,
  "surviving_mutations": 2,
  "mutation_score": 0.75,
  "limits": { "required_removed": 50, "enum_narrowed": 10, "minimum_increased": 50, "...": 50 },
  "rules": [
    { "rule": "required_removed", "total": 2, "killed": 1,
      "surviving": 1, "mutation_score": 0.5 },
    { "rule": "enum_narrowed", "total": 3, "killed": 2,
      "surviving": 1, "mutation_score": 0.6666666666666666 },
    { "rule": "minimum_increased", "total": 1, "killed": 1,
      "surviving": 0, "mutation_score": 1.0 },
    { "rule": "max_length_decreased", "total": 0, "killed": 0,
      "surviving": 0, "mutation_score": null }
  ],
  "mutants": [
    {
      "id": "d9cfcfa78d76",
      "rule": "enum_narrowed",
      "pointer": "#/properties/status",
      "subject": "\"pending\"",
      "description": "枚举收窄：删除取值 \"pending\"",
      "killed": true,
      "example_indices": [0]
    },
    {
      "id": "13f4ce505949",
      "rule": "required_removed",
      "pointer": "#",
      "subject": "status",
      "description": "删除必填字段 \"status\"（required 移除该项）",
      "killed": false,
      "example_indices": []
    }
  ],
  "uncovered_paths": ["#/properties/status", "#"]
}
```

响应字段：

| 字段                          | 说明                                                            |
| ----------------------------- | --------------------------------------------------------------- |
| `mutation_score`              | 总体变异覆盖率 = `killed_mutations / total_mutations`；无变异体时为 `1.0` |
| `valid_documents` / `invalid_documents` | 通过 / 被原新 schema 拒绝的文档数；放松类变异由后者发现  |
| `rules[]`                     | **按规则分类**的统计：每类的变异数、杀死数、存活数与分类 score；该类无变异体时 score 为 `null` |
| `limits`                      | 本次评估各类变异实际生效的生成上限（缺省规则回填为 50）         |
| `mutants[]`                   | 全部变异体的逐变异结果，按 (规则, 路径, 主体, ID) 确定性排序    |
| `mutants[].id`                | 变异体稳定 ID：`rule + 节点指针 + 变异主体` 的 SHA-256 前 12 位 |
| `mutants[].pointer`           | 变异施加的 schema 节点位置（JSON Pointer，根为 `#`）            |
| `mutants[].subject`           | 变异主体：必填字段名 / 被删枚举值 / 被删类型 / 关键字名         |
| `mutants[].killed`            | 是否被提交样本发现（原 / 异 schema 校验结果不同）               |
| `mutants[].example_indices`   | 杀死该变异体的示例文档下标（从 0 开始，升序，最多保留 20 个）   |
| `uncovered_paths`             | **未覆盖变异路径**：所有存活变异体的 schema 指针（可能重复）    |

收紧类变异（枚举收窄、范围收紧等）由**原 schema 合法、变异后被拒绝**
的文档发现；放松类变异（删除必填项）由**原 schema 拒绝、变异后合法**
的负向文档发现（如恰好缺少该必填字段的脏数据），因此提交的样本集
越能代表真实数据的边界取值，mutation score 越能反映样本的规则覆盖能力。

入参为空列表、超过 5000 个文档、缺少 `documents` 字段、`max_per_rule`
含未知规则名或负数（超过 10000）时返回 `422`。

### `POST /api/analyses/{id}/redact` — 按 `x-sensitive` 标记脱敏文档实例

读取该分析的**新 Schema**（`new_schema`），对一批提交的 JSON 文档做
schema 与实例的并行遍历：**仅当**实例位置对应的 schema 节点显式声明
`"x-sensitive": true` 时，把该实例值替换为固定掩码 `"***"`，其余内容
原样保留。接口为**纯只读**操作：脱敏只作用于**提交文档的深拷贝**，
不修改请求原文，也不改动库中保存的分析、旧/新 schema 或样本；分析
编号不存在返回 `404`。

- **对象 / 数组遍历**：`properties`、`patternProperties`、
  `additionalProperties`、`items`（数组写法）、`prefixItems` 与
  draft-04/06/07 元组写法 `items` + `additionalItems` 均按实例的实际
  结构下钻；
- **本地 `$ref`**：解析文档内 `#` / `#/...` JSON Pointer 片段引用
  （RFC 6901，支持 `~0` / `~1` 与 `%2F` 转义），不访问网络或外部文件；
- **组合关键字**：`oneOf` / `anyOf` **只遍历对当前实例（原值）校验
  通过的分支**，`allOf` 遍历**全部分支**；分支判定基于尚未脱敏的
  原始实例值，敏感字段自身被掩码不会改变分支路由；
- **递归引用终止**：同一实例位置上同一引用目标只展开一次，`#` 自指、
  定义互引、链表式自引用均安全终止；进入子属性 / 数组元素后重新计数，
  因此数组每个元素都会按其元素 schema 正常脱敏；
- **无法解析的引用**（外部引用、`#anchor` 名称片段、目标不存在、非法
  指针转义、非字符串 `$ref`）返回 **`422`**，响应体带声明该引用的
  **schema 指针**（`schema_pointer`）与当前实例指针。

请求体：

| 字段        | 类型  | 必填 | 说明                                            |
| ----------- | ----- | ---- | ----------------------------------------------- |
| `documents` | any[] | 是   | 待脱敏 JSON 文档列表（任意类型），数量 1..5000    |

```bash
curl -X POST http://localhost:8000/api/analyses/1/redact \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {
      "name": "alice",
      "ssn": "111-22-3333",
      "tags": ["vip"],
      "profile": {"email": "alice@example.com"},
      "record": {"kind": "a", "secret": "shh"}
    },
    {"name": "bob", "ssn": "444-55-6666", "record": {"kind": "b"}}
  ]}'
```

上例对应新 schema（创建分析时提交的 `new_schema`）：

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "ssn": {"type": "string", "x-sensitive": true},
    "tags": {"type": "array", "items": {"x-sensitive": true}},
    "profile": {"$ref": "#/$defs/Profile"},
    "record": {
      "oneOf": [
        {"type": "object",
         "properties": {"kind": {"const": "a"}, "secret": {"x-sensitive": true}},
         "required": ["kind"]},
        {"type": "object",
         "properties": {"kind": {"const": "b"}},
         "required": ["kind"]}
      ]
    }
  },
  "$defs": {
    "Profile": {
      "type": "object",
      "properties": {"email": {"x-sensitive": true}}
    }
  }
}
```

响应（按输入顺序返回脱敏文档与替换位置的实例指针）：

```json
{
  "analysis_id": 1,
  "total_documents": 2,
  "results": [
    {
      "index": 0,
      "redacted": {
        "name": "alice",
        "ssn": "***",
        "tags": ["***"],
        "profile": {"email": "***"},
        "record": {"kind": "a", "secret": "***"}
      },
      "replacements": [
        {"instance_pointer": "#/profile/email",
         "schema_pointer": "#/$defs/Profile/properties/email"},
        {"instance_pointer": "#/record/secret",
         "schema_pointer": "#/properties/record/oneOf/0/properties/secret"},
        {"instance_pointer": "#/ssn", "schema_pointer": "#/properties/ssn"},
        {"instance_pointer": "#/tags/0", "schema_pointer": "#/properties/tags/items"}
      ]
    },
    {
      "index": 1,
      "redacted": {"name": "bob", "ssn": "***", "record": {"kind": "b"}},
      "replacements": [
        {"instance_pointer": "#/ssn", "schema_pointer": "#/properties/ssn"}
      ]
    }
  ]
}
```

响应字段：

| 字段                              | 说明                                                         |
| --------------------------------- | ------------------------------------------------------------ |
| `results[]`                       | 与提交顺序一一对应的逐文档结果（`index` 为从 0 开始的下标）    |
| `results[].redacted`              | 脱敏后的文档（入参文档的深拷贝；仅命中位置被替换为 `***`）     |
| `results[].replacements[]`        | 替换位置列表，按实例指针确定排序，同一实例位置最多一条         |
| `replacements[].instance_pointer` | 被替换实例值的位置（JSON Pointer，根为 `#`，如 `#/tags/0`）    |
| `replacements[].schema_pointer`   | 声明 `x-sensitive: true` 的 schema 节点位置（如 `#/$defs/Profile/properties/email`） |

判定细则：

- 只有字面布尔值 `true` 才触发脱敏（`"x-sensitive": false` / `"yes"` /
  其他类型一律忽略）；祖先节点命中时整体掩码，不再重复记录其后代位置；
- `oneOf` / `anyOf` 的多个分支同时校验通过时（如分支约束互不排斥），
  这些分支都会遍历；`allOf` 的分支即使对实例校验失败也照常遍历
  （`allOf` 是叠加约束而非候选路由）；
- 对象键中含 `/`、`~` 时实例指针按 RFC 6901 转义（`a/b` → `#/a~1b`，
  `m~n` → `#/m~0n`），指针可直接定位回原文档；
- 入参为空列表、超过 5000 个文档或缺少 `documents` 字段时同样返回
  `422`（Pydantic 校验错误）；该接口不产生任何持久化记录，重复调用
  结果完全一致。

### `POST /api/analyses/{id}/counterexample-reduction` — 兼容性反例缩减

接收**单份** JSON 文档与一个兼容破坏方向（`backward` / `forward`），先用
分析记录保存的**旧、新两版 schema** 校验文档，确认它确实属于指定的单向
破坏类别（`backward`：旧版合法、新版拒绝；`forward`：新版合法、旧版拒绝），
然后在**保持该方向不变**的前提下，按固定顺序做删除式贪心缩减，得到更小
的反例。接口为**纯只读**操作：缩减只作用于提交文档的深拷贝，不修改分析
记录、旧/新 schema、报告或样本，也不写任何历史；分析编号不存在返回 `404`。

请求体：

| 字段              | 类型    | 必填 | 说明                                                |
| ----------------- | ------- | ---- | --------------------------------------------------- |
| `document`        | any     | 是   | 待缩减的**单份** JSON 文档（任意 JSON 类型）          |
| `direction`       | string  | 是   | 需要保持的破坏方向：`backward` / `forward`            |
| `max_validations` | int     | 否   | 候选**重验**次数上限，缺省 `200`，范围 `1..10000`      |

缩减操作按**固定顺序**枚举候选，每接受一步就从上一个接受点重新枚举
（贪心下降；对象键按字典序，数组按下标从小到大，字符串按先序位置并从
末位字符开始删除）：

1. **删除对象属性**（`delete_property`）：根节点自身属性优先，再逐层
   深入嵌套对象；
2. **删除数组元素**（`delete_element`）：按下标从小到大，包括嵌套在
   对象 / 数组中的数组；
3. **缩短字符串**（`shorten_string`）：按字符串在文档中的先序位置，
   对每个字符串从最后一个字符开始逐个删除。

每个候选只有同时满足以下两个条件才会被接受并记录一步操作：

- **序列化长度严格变小**：与当前文档使用同一紧凑 JSON 序列化
  （`json.dumps(..., sort_keys=True, separators=(",", ":"))`）比较；
- **重验两版后仍保持指定方向**：用旧、新 schema 各重新校验一次
  （一次"重验"），文档仍属于请求的单向破坏类别。

循环直到一轮枚举中没有候选可接受（不能再缩减），或重验次数达到
`max_validations` 上限。**达到上限时立即停止，只返回最后一次已验证
通过的文档**，绝不返回未经验证的候选；`limit_reached: true` 标识因
上限提前结束。

```bash
# 旧版：s 必填、非空字符串；新版：s 必填、枚举只剩 "a"
# {"s": "bbb", ...} 旧版合法、新版拒绝 -> backward 反例
curl -s -X POST http://localhost:8000/api/analyses/1/counterexample-reduction \
  -H "Content-Type: application/json" \
  -d '{
    "document": {"s": "bbb", "noise": {"a": 1, "b": 2}, "zzz": [1, 2, 3]},
    "direction": "backward"
  }' | python3 -m json.tool
```

响应：

```json
{
  "analysis_id": 1,
  "direction": "backward",
  "original": {"s": "bbb", "noise": {"a": 1, "b": 2}, "zzz": [1, 2, 3]},
  "reduced": {"s": "b"},
  "steps": [
    {"index": 0, "op": "delete_property", "pointer": "#/noise",
     "key": "noise", "element_index": null, "position": null,
     "char": null, "from_length": null, "to_length": null,
     "serialized_length_before": 47, "serialized_length_after": 25,
     "validations_used": 1},
    {"index": 1, "op": "delete_property", "pointer": "#/zzz",
     "key": "zzz", "element_index": null, "position": null,
     "char": null, "from_length": null, "to_length": null,
     "serialized_length_before": 25, "serialized_length_after": 11,
     "validations_used": 3},
    {"index": 2, "op": "shorten_string", "pointer": "#/s",
     "key": null, "element_index": null, "position": 2,
     "char": "b", "from_length": 3, "to_length": 2,
     "serialized_length_before": 11, "serialized_length_after": 10,
     "validations_used": 5},
    {"index": 3, "op": "shorten_string", "pointer": "#/s",
     "key": null, "element_index": null, "position": 1,
     "char": "b", "from_length": 2, "to_length": 1,
     "serialized_length_before": 10, "serialized_length_after": 9,
     "validations_used": 7}
  ],
  "validation_count": 9,
  "max_validations": 200,
  "limit_reached": false,
  "serialized_length_original": 47,
  "serialized_length_reduced": 9}
```

上例第一轮枚举中，对象属性按字典序依次是 `noise`、`s`、`zzz`：
删 `noise` 重验通过被接受（重验 1 次）；从剩余文档重新枚举后，删 `s`
会使文档缺少旧版必填字段（方向丢失），被拒绝（重验 2 次），随后删
`zzz` 才被接受（重验 3 次）。字符串 `"bbb"` 的缩短轨迹为：删末位 `b`
得 `"bb"`（仍破坏，接受），再删末位 `b` 得 `"b"`（仍破坏，接受）；
随后尝试删 `"b"` 得到 `""` 会被旧版 `minLength: 1` 拒绝（方向丢失），
该候选被拒绝，缩减终止。**被拒绝的候选不进入 `steps`，但仍消耗重验
次数**（故 `validation_count` 为 9 而不是 4）。对象属性删除始终先于
数组元素与字符串缩短尝试，因此最终结果是 `{"s": "b"}`。

响应字段：

| 字段                              | 说明                                                            |
| --------------------------------- | --------------------------------------------------------------- |
| `original`                        | 提交的原始文档（不被修改；结果中为独立副本）                     |
| `reduced`                         | 缩减后的文档；每一步都经旧、新两版重验且仍属于指定方向            |
| `steps[]`                         | 每一步**被接受**的操作记录（按接受先后，序号从 0 连续）           |
| `steps[].op`                      | `delete_property` / `delete_element` / `shorten_string` 三者之一 |
| `steps[].pointer`                 | 操作目标在文档中的位置（JSON Pointer，根为 `#`）                  |
| `steps[].key`                     | `delete_property` 时被删属性名；其他操作为 `null`                 |
| `steps[].element_index`           | `delete_element` 时被删元素下标；其他操作为 `null`                |
| `steps[].position` / `char`       | `shorten_string` 时被删字符的位置（从 0 开始）与该字符            |
| `steps[].from_length`/`to_length` | `shorten_string` 时缩短前 / 后的字符串长度                        |
| `steps[].serialized_length_before/after` | 该步前 / 后文档的紧凑 JSON 长度（`after` 严格更小）       |
| `validation_count`                | 实际执行的候选重验次数（旧、新各校验一次算一次；入口分类校验不计） |
| `max_validations`                 | 本次生效的重验上限                                               |
| `limit_reached`                   | 是否因达到上限提前结束；为 `true` 时 `reduced` 也是已验证结果      |

入口文档不属于指定的单向破坏类别时返回 **`422`**，响应体给出明确原因、
请求方向以及文档对旧 / 新两版的校验结果：

| `detail.reason`     | 含义                                         |
| ------------------- | -------------------------------------------- |
| `valid_both`        | 文档对旧、新两版均合法，不构成任何反例        |
| `invalid_both`      | 文档被旧、新两版同时拒绝（脏数据 / 共有约束） |
| `wrong_direction`   | 文档属于相反方向的破坏类别，附 `actual_direction` |

```bash
curl -s -X POST http://localhost:8000/api/analyses/1/counterexample-reduction \
  -H "Content-Type: application/json" \
  -d '{"document": {"s": "a"}, "direction": "backward"}'
# 422
# {"detail": {
#   "message": "文档对旧版与新版 schema 均校验通过，不属于任何单向破坏类别",
#   "requested_direction": "backward",
#   "valid_against_old": true,
#   "valid_against_new": true,
#   "reason": "valid_both"
# }}
```

缺少 `document` / `direction` 字段、方向不是 `backward` / `forward`、
`max_validations` 越界（`<1` 或 `>10000`）时返回 `422`（Pydantic 校验错误）。
该接口不产生任何持久化记录，重复调用结果完全一致。

### `POST /api/reference-diagnostics` — `$ref` 引用依赖诊断

接收**单份** JSON Schema，遍历其内部全部 `$ref`，按 **JSON Pointer
（RFC 6901）在本文档内解析本地目标**，输出引用边、不可达的 `$defs`
节点、循环链与无效目标的定位信息。接口为**纯只读**操作：**不创建
分析记录、不写数据库**，也**绝不访问网络或外部文件**——只接受片段
形式的本地引用（`#` 或 `#/...`）。

请求体：

| 字段     | 类型   | 必填 | 说明                                 |
| -------- | ------ | ---- | ------------------------------------ |
| `schema` | object | 是   | 待诊断的单份 JSON Schema（JSON 对象） |

```bash
curl -X POST http://localhost:8000/api/reference-diagnostics \
  -H "Content-Type: application/json" \
  -d '{
    "schema": {
      "type": "object",
      "properties": {
        "name": {"$ref": "#/$defs/Name"},
        "tree": {"$ref": "#/$defs/Node"},
        "root": {"$ref": "#"},
        "bad":  {"$ref": "#/$defs/Missing"},
        "ext":  {"$ref": "https://example.com/s.json#/$defs/X"}
      },
      "$defs": {
        "Name": {"type": "string"},
        "Node": {
          "type": "object",
          "properties": {"child": {"$ref": "#/$defs/Node"},
                         "peer": {"$ref": "#/$defs/A"}}
        },
        "A": {"$ref": "#/$defs/B"},
        "B": {"$ref": "#/$defs/A"},
        "Orphan": {"type": "null"}
      }
    }
  }'
```

响应（节选）：

```json
{
  "counts": {
    "refs": 9,
    "edges": 7,
    "invalid_refs": 2,
    "unreachable_defs": 1,
    "cycles": 3
  },
  "edges": [
    { "source": "#/$defs/A", "ref": "#/$defs/B", "target": "#/$defs/B" },
    { "source": "#/$defs/B", "ref": "#/$defs/A", "target": "#/$defs/A" },
    { "source": "#/$defs/Node/properties/child", "ref": "#/$defs/Node", "target": "#/$defs/Node" },
    { "source": "#/$defs/Node/properties/peer", "ref": "#/$defs/A", "target": "#/$defs/A" },
    { "source": "#/properties/name", "ref": "#/$defs/Name", "target": "#/$defs/Name" },
    { "source": "#/properties/root", "ref": "#", "target": "#" },
    { "source": "#/properties/tree", "ref": "#/$defs/Node", "target": "#/$defs/Node" }
  ],
  "invalid_refs": [
    {
      "source": "#/properties/bad",
      "ref": "#/$defs/Missing",
      "reason": "target_not_found",
      "message": "引用目标 #/$defs/Missing 在路径段 'Missing' 处不存在"
    },
    {
      "source": "#/properties/ext",
      "ref": "https://example.com/s.json#/$defs/X",
      "reason": "external_reference",
      "message": "仅接受片段形式的本地引用（# 或 #/...），不访问网络或外部文件"
    }
  ],
  "unreachable_defs": [
    { "pointer": "#/$defs/Orphan", "name": "Orphan" }
  ],
  "cycles": [
    { "chain": ["#/$defs/A", "#/$defs/B", "#/$defs/A"] },
    { "chain": ["#/$defs/Node", "#/$defs/Node"] },
    { "chain": ["#/properties/root", "#"] }
  ]
}
```

响应字段：

| 字段                              | 说明                                                                 |
| --------------------------------- | -------------------------------------------------------------------- |
| `counts.refs`                     | `$ref` 出现总数（可解析边数 + 无效引用数）                           |
| `edges[]`                         | 可解析的本地引用边，按 `source` 排序                                 |
| `edges[].source`                  | 声明该 `$ref` 的 schema 节点位置（JSON Pointer，根为 `#`）           |
| `edges[].target`                  | 解析并**规范化**后的目标位置（`%2F` 解码、指针转义归一）             |
| `invalid_refs[]`                  | 不可用引用的定位与原因，按 `source` 排序                             |
| `unreachable_defs[]`              | 从根侧沿引用链**不可达**的 `$defs` 节点（死定义），按指针排序        |
| `unreachable_defs[].pointer/name` | 节点位置与其所属 `$defs` 容器内的键名（含定义内嵌套 `$defs` 节点）   |
| `cycles[].chain`                  | 循环链：首尾相接的节点指针链；以链上最小指针旋转到链首，唯一且确定    |

`invalid_refs[].reason` 取值：

| reason                       | 含义                                                                 |
| ---------------------------- | -------------------------------------------------------------------- |
| `external_reference`         | 非片段形式的引用（`http(s)://…`、相对文件路径、裸名称），**不会**去解析 |
| `name_fragment_unsupported`  | `#anchor` 形式的名称片段（锚点）；仅支持 `#/...` 指针片段             |
| `target_not_found`           | 指针在本文档内无法解析（键不存在 / 数组越界 / 前导零下标 / `-` 等）   |
| `malformed_pointer`          | 非法 JSON Pointer 转义（`~` 后只能为 `0` 或 `1`）                     |
| `non_string_ref`             | `$ref` 的值不是字符串                                                 |

判定细则：

- **只接受本地片段引用**：`#` 指向文档根，`#/a~1b` 等按 RFC 6901 解析；
  支持 `~0` / `~1` 转义与 URI 百分号编码（`%2F` 解码后与 `~1` 归一）；
- **可达性**：以"源在所有 `$defs` 节点之外（根侧结构）"的引用边为种子
  做 BFS，再沿已到达定义**内部**发出的全部引用边传播；只能从其它死
  定义间接到达、根侧引用链触达不到的节点进入 `unreachable_defs`；
  定义内嵌套 `$defs` 的子节点同样参与判定（未被任何引用显式指向即为
  死定义）；
- **循环链**：定义节点取自身 `$ref` 优先的代表出边追踪成环，树节点
  内部属性引用自身定义形成自环（`["#/$defs/Node", "#/$defs/Node"]`），
  根侧节点引用 `#` 报告为回边；同一环只出现一次，结果与输入键顺序无关；
- `enum` / `const` / `default` / `examples` 中的 `$ref` 是**实例数据**，
  不计入引用图。

提交的 schema 本身不合法时返回 `400`（与创建分析一致）；缺少 `schema`
字段或其值不是 JSON 对象时返回 `422`。该接口不产生任何持久化记录，
重复调用结果完全一致。

### `GET /health`

```json
{ "status": "ok" }
```

---

## issue 代码一览

| code | 触发场景 |
| ---- | -------- |
| `type_removed` / `type_restricted` | 类型被移除或收窄 |
| `required_added` | 字段在新版变为必填 |
| `enum_value_removed` / `enum_added` | 枚举值被删除 / 新增枚举约束 |
| `const_added` / `const_changed` | 新增常量约束 / 常量值变化 |
| `property_no_longer_allowed` | 属性被删除且不允许额外字段 |
| `additional_properties_forbidden` | `additionalProperties` 收紧为 false |
| `min/max_properties_*` | 对象属性数量边界变化 |
| `min/max_items_*` | 数组长度边界变化 |
| `unique_items_added` | 新增元素唯一性要求 |
| `minimum_increased` / `maximum_decreased` | 数值边界收紧 |
| `multiple_of_added` / `integer_required` | 新增整除 / 整数要求 |
| `min/max_length_*` / `pattern_changed` | 字符串长度、正则变化 |
| `anyOf/oneOf_branch_missing` / `_added` / `_rejected` | 组合分支不兼容 |
| `not_added` | 新增 not 否定约束 |
| `if_condition_changed` | if 条件表达式变化，实例可能被路由到不同 then/else 分支 |
| `schema_forbids_all` | 目标位置为 `false`，拒绝所有实例 |
| `sample_rejected_by_new/old` | 提供的真实样例被某一版拒绝 |

---

## 实现说明

引擎核心是单向蕴含检查 `find_violations(source, target)`：递归找出所有
"source 允许、target 拒绝"的结构，为每个结构生成一个位于该路径的最小片段，
再沿 schema 路径反向包裹成完整文档（过程中自动补齐 `required`、数组前缀、
`minItems` 等约束），最后用 `jsonschema` 双向验证。交换入参即可得到另一个
兼容方向，所以所有规则只需实现一遍。

遇到 `if` / `then` / `else` 条件节点时，引擎在源 schema 的副本上把该节点
替换为"rest + 激活分支"的构造用 schema，按激活单元派生子比较；子比较
构造反例时会按需强制实例满足 / 违反对应的 `if`，并启用验证门禁——
只有经验证的反例才记 `error`，否则统一降级 `warning`。

变异覆盖评估（`mutation.py`）是上述只读分析链路的补充：它不比较两版
schema，而是对**新 schema 自身**做单点变异——`$ref` 预解析后递归遍历
对象、数组、组合分支与条件分支，在每个 schema 节点上按声明的关键字
确定性地产出"删除必填项 / 收窄枚举 / 收紧数值与长度范围 / 收窄类型"
等变异描述，先在节点局部用区分见证剔除等价变异、再按每类上限稳定
截断；随后逐变异地把单点编辑施加到 schema 深拷贝上，用 `jsonschema`
对每篇文档比对原 / 异校验结果，汇总出按规则分类的 mutation score、
未覆盖变异路径与杀死变异的样例下标。整个评估无随机源、不依赖文档
顺序，也不产生任何持久化写入。
