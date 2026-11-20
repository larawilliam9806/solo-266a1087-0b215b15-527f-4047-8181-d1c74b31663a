# 纾困户数季度公报 API

按季度发布「区县 × 行业」纾困户数交叉表。区县合计与行业合计由编辑凭令牌登记后**固定**；
明细以最多 8×8 的非负整数矩阵提交，必须与已公布的合计一致；对外只公开合计与被披露的格子。
1～4 户的格子强制遮蔽，并保证每个被遮蔽的格子都至少有两种可能的非负整数值。

尚未发布的季度，上级可以**修订**区县与行业合计：每次整份换交生成新的登记版号，
旧登记与旧底表全部留档。技术栈：FastAPI + SQLite（标准库 `sqlite3`），零 ORM，
`docker compose up` 即可启动。

---

## 快速启动

```bash
docker compose up --build -d
# 首次启动（空库）会在日志里打印一次性编辑令牌：
#   [bulletin] bootstrap editor token: ed_xxxxxxxx
docker compose logs bulletin | grep 'bootstrap editor token'
```

随后用该令牌调用编辑接口（`Authorization: Bearer ed_xxxxxxxx`）。令牌只存 SHA-256，
明文只在启动日志出现一次；可在容器内用 `POST /api/v1/editor/tokens` 另签。

数据库落在命名卷 `bulletin-data`（`/data/bulletins.sqlite3`），容器重建不丢数据。
镜像内以 `/health` 做健康检查。

本地开发：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload      # 或 BULLETIN_DB=./data/x.sqlite3 uvicorn app.main:app
pytest                             # 测试
```

接口文档：`http://localhost:8000/docs`。

---

## 业务规则

### 1. 合计登记后固定

`PUT /api/v1/editor/quarters/{quarter}/totals`

* 登记区县列表、行业列表及各自的合计。两个方向的合计之和必须相等，否则 422 `totals_inconsistent`。
* 登记成功后该季度合计不可再次登记：再次 `PUT` 返回 409 `totals_already_registered`。
  需要纠正时走第 1a 节的修订接口，而不是重新登记。
* 维度上限 8×8。

### 1a. 合计修订（未发布季度）

`POST /api/v1/editor/quarters/{quarter}/totals/revisions`

凭原编辑令牌、带**所见登记版号**（`expected_registration_version`），整份换交新的区县
及行业合计；校验规则与初次登记完全一致（严格非负整数、轴内名称唯一、双向合计之和相等）。
登记从未修订（仍是第 1 版）时可省略版号；一旦修订过，省略版号返回
409 `registration_version_required`，版号对不上返回 409 `stale_registration_version`。

修订分两种形态，响应里以 `change_kind` 标明：

* **`reorder`（仅调行列顺序）**：区县/行业名称集合与每个名称对应的户数都没变，只是顺序变了。
  服务按名称把当前底表重排到新位置，**必公开格和已有遮盖一并平移**，并在新登记下生成一份
  新明细版号（有遮盖时同时生成绑定它的遮盖计划）。新底表的边际与新登记天然一致，无需重交。
* **`breaking`（名称或对应户数变化）**：有名称增删或某名称的合计变化。旧明细对不上新边际，
  只作留档，不再出现在可用明细版号里；编辑必须按新登记重新提交底表后才能继续遮盖与发布。

其余约束：

* **已发公报的季度拒绝修订**：409 `bulletin_already_published`，公开快照不受影响。
* 每次成功修订只消费完整的一版，登记版号自增；旧登记在 `totals_revisions` 中留档，
  旧底表与遮盖随旧明细版号留档，互不覆盖。
* 修订在单个 `BEGIN IMMEDIATE` 事务内完成；任何校验失败都会回滚，原登记、底表和遮盖继续有效。
* 修订与提交/遮盖/发布交错时，每次成功操作都必须基于同一个完整登记版号，
  版号守卫先于其它校验生效。

### 2. 明细提交

`POST /api/v1/editor/quarters/{quarter}/detail`

* `values` 必须是严格非负整数（`-1`、`3.5`、`true`、`"3"` 一律 422）。
* 行和必须等于**当前登记**的区县合计，列和必须等于当前登记的行业合计；不一致时返回
  422 `totals_mismatch`，并在 `details.rows` / `details.columns` 指出具体行列及差额。
* **合计对不上时不会写入**，上一份有效明细原样保留（版本号不变）。
* 每次成功提交生成新版本（`version` 自增，版号在整个季度内只增不改），并记录它所属的
  登记版号；可用 `expected_version` 做明细乐观并发，用 `expected_registration_version`
  核对所见登记版号（第 1 版可省略，之后必填）。
* `must_public` 指定必须公开的格子（`[[row, col], ...]` 或 `[{"row":r,"col":c}, ...]`）；
  1～4 户的格子不允许被要求公开（409 `sensitive_cell_must_be_masked`）。

### 3. 遮盖（遮蔽）编辑

`PUT /api/v1/editor/quarters/{quarter}/mask`

* 1～4 户的格子未遮蔽 → 409 `mask_conflict`，`details.cells` 列出所有冲突格子。
* 必须公开的格子被遮蔽 → 同样 409，`issues[].kind = must_public_cell_masked`。
* 遮盖计划绑定到明细版本：明细一变，旧计划即失效（不会被误用于新明细）。
* 遮盖可反复调整，每次 `mask_version` 自增；返回体里带当前遮盖的分析结果（可能仍有保护缺口，
  此时 `analysis.valid=false`，不阻止保存，但发布时会被拦下）。
* 请求需带 `expected_registration_version`（第 1 版可省略）；遮盖只能针对当前登记下的底表。

### 4. 补遮建议

`POST /api/v1/editor/quarters/{quarter}/suppression-advice`

```json
{ "detail_version": 1, "cells": [{"row": 0, "col": 0}] }   // cells 可省略：取当前遮盖
```

建议**只针对当前登记下的底表**：修订后若没有可用明细（结构性修订尚未重交），返回
409 `detail_required`；点名旧明细版号返回 409 `stale_detail_version`。

返回：

```json
{
  "valid": true,
  "required_mask":  [{"row":0,"col":0}],          // 规则强制（1～4 户）
  "supplementary_mask": [{"row":0,"col":1}, ...], // 补遮建议
  "masked": [...],                                 // 建议后的完整遮盖
  "irredundant": true,
  "unresolved": [],
  "analysis": { "valid": true, "protected": { "0,0": {"min":0,"max":12} } }
}
```

**补遮建议单独公开其中任一格都会破坏「至少两种可能取值」的保证**（`irredundant: true`）：
建议集合在保持建议后方案有效的前提下，逐格做了必要性检验，去掉任何一格都会让至少一个
受保护格退化为唯一值。无法补全时 `valid=false` 并在 `unresolved` 里给出原因与格子。

### 5. 发布

`POST /api/v1/editor/quarters/{quarter}/publish`

* 请求需带 `expected_registration_version`（第 1 版可省略），发布前**重新复核**：
  仍存在冲突（1～4 户未遮、必须公开的被遮、某遮蔽格只有唯一取值）→
  409 `mask_conflict`，`details.cells` 指出冲突格子，并提示调用补遮建议。
* 遮盖计划属于过期明细 → 409 `mask_plan_superseded`，并在 `details` 里给出旧方案与旧明细版本
  （只在同一登记版号内追溯旧计划，留档于旧登记的遮盖不会被误报）。
* `mask_version` 与当前不一致 → 409 `stale_mask_version`。
* **每季只发一份**：已发布后再次发布 → 409 `already_published`；已发布季度的合计修订
  → 409 `bulletin_already_published`。发布失败不会影响已发布的公报，
  `GET /api/v1/bulletins/{quarter}` 照常可查。
* 发布是事务性的：校验、快照写入、唯一约束在同一 `BEGIN IMMEDIATE` 事务里完成，并发也不会有第二份。

### 6. 公开查询

`GET /api/v1/bulletins/{quarter}`（公开，无需令牌；不存在时 404，季度格式不合法时 422）

```json
{
  "quarter": "2024Q1",
  "districts": [{"name": "东城区", "total": 30}, ...],
  "industries": [{"name": "制造业", "total": 40}, ...],
  "row_totals": [30, 25, 10, 50],
  "col_totals": [40, 45, 30],
  "grand_total": 115,
  "cells": [[10, null, null], [null, null, 9], ...],
  "published_at": "2024-04-15T09:00:00+00:00"
}
```

`cells` 中 `null` 表示未披露。响应里**不含**明细版本、遮盖计划、`must_public`、
或任何未披露的数值。

`GET /api/v1/bulletins` 列出已发布季度及总计。

### 7. 编辑查询（含版号）

`GET /api/v1/editor/quarters/{quarter}`（需令牌）返回编辑工作视图，其中：

* `registration_version`：当前登记版号；`registration_history` 列出每次登记/修订
  （`version`、`change_kind` 为 `initial` / `reorder` / `breaking`、时间）。
* `detail_versions`：**当前登记下可用的明细版号**（每条带所属 `registration_version`）；
  旧登记的明细留档但不在此列，`current_detail_version` 指向当前最新。
* `mask_version` / `masked_cells` / `analysis`：当前底表上的遮盖与复核结果。
* `bulletin_registration_version`：已发布快照对应的登记版号（未发布为 `null`）。

---

## 保密性判定（为什么是精确的，不是启发式）

公开区县合计、行业合计与所有可见格后，每个被遮蔽格的取值被约束在一个整点运输多面体里。
真实明细是该多面体的一个整点，任何其它可行点与它之差是一个「整圈流」。

* 让某格 `e` 增加 `t`：需要一条从 `e` 所在列回到 `e` 所在行的残量通路，
  经过其它遮蔽格时只能减少（受其当前值限制）；
* 让 `e` 减少：方向相反，另外受 `e` 自身取值限制。

求最大 `t` 就是一次最大流（本实现为 16 个节点内的 Dinic），因此每格的最小/最大可行值
是**精确整数界**。只要 `min < max`，该格就至少有两种可能取值。
测试里用暴力枚举所有可行矩阵做了交叉验证（`tests/test_api.py::test_suppression_bounds_match_brute_force`）。

补遮建议的搜索：优先加入能最大程度减少冲突的格，随后做逐格必要性剔除。

---

## 鉴权

* 空库首次启动自动生成 bootstrap 令牌，只打印一次，库里只存 SHA-256。
* 所有编辑接口要求 `Authorization: Bearer <token>`；缺失 401，无效/吊销 403。
* `POST /api/v1/editor/tokens` 可再签发令牌（`{"label": "..."}`），返回明文一次，之后不可再取。

## 目录结构

```
app/
  main.py        FastAPI 路由与依赖
  models.py      Pydantic 请求模型（严格整数、单元格两种写法）
  service.py     业务规则与版本/发布事务
  suppression.py 精确的遮蔽分析（最大流）与补遮建议
  db.py          SQLite 连接、schema、事务
tests/           pytest 端到端测试
Dockerfile / docker-compose.yml
```

## 设计取舍

* **合计按登记版号留痕，不静默改数**：初次登记固定为第 1 版；未发布季度的纠正通过修订接口
  生成新登记版号，旧登记、旧底表与旧遮盖全部留档。已发布季度代表数字已公开，一律拒绝修订。
* **重排即平移，结构性变更才重交**：只动行列顺序时按名称重排当前底表，必公开格与遮盖一起移动，
  避免无谓的重新编制；名称或户数一变，旧边际失效，强制重交底表，杜绝新旧边际混用。
* **版号守卫统一**：提交明细、保存遮盖、发布、修订都核对所见登记版号；第 1 版可省略，
  修订后必填，交错操作时每次成功调用只落在一个完整版本上。
* **遮盖计划绑定明细版本**：明细变了就不再有任何「旧遮盖可复用」的歧义，发布时能明确报出
  `mask_plan_superseded`。
* **保存与发布分离**：编辑可以先保存一份尚不完整的遮盖（带 `analysis.valid=false` 提示），
  但发布必须完整合规，复核在发布瞬间进行。
* **公开载荷最小化**：公开接口只返回合计与可见格；登记版号、明细版本、遮盖计划、必公开清单
  等内部状态不出现在响应里。
* **平滑迁移**：旧版本数据库启动时自动补列并回填第 1 版登记档案，docker 命名卷里的存量数据
  无需手工处理。
