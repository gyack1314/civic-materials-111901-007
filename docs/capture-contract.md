# 采集端字段与人工确认契约

本文件描述 `capture` 产物的字段含义和人工确认流程，与
[`source-contract.md`](source-contract.md) 中的来源契约配套。来源契约定义原件如何声明
自己；本文件定义采集端如何记录"采用了什么、为什么、到哪里回查"。

## 1. 运行目录

一个运行目录对应一次采集（`--run-dir`）：

```text
<run-dir>/
  manifest.json          目录响应、原件 URL/字节数/sha256/ETag、请求记录
  originals/             下载的 PDF 与工作簿（不改写）
  dataset.json           采集记录（权威产物）
  observations.csv       城市年度记录（扁平表）
  reviews.md             人工核实清单 + 双载体核对
  report.html            单文件回查页
  reviews.json           仅核实项，便于程序处理
  decisions.json         人工确认（可选）
  deliveries/            每次重写交付前的时间戳快照
```

`dataset.json` 的 `source.records` 保存目录原始条目（含 `publication_year`），
`declarations` 保存每个原件自己声明的版本、版次和勘误编号。出版年只出现在这两处，
观测年只出现在记录字段中。

## 2. 记录（`records[]`）

| 字段 | 含义 |
| --- | --- |
| `key` | `城市代码｜观测年｜指标`，一条城市数据的身份 |
| `city_code` / `city_name` | 原件行标签；`P00` 等省合计不会出现在这里 |
| `observation_year` | 表头年份，不是出版年 |
| `indicator` | 该列所属的合并表头文本，如"终端能源" |
| `unit` | 被采用读数所在表/单元格声明的单位 |
| `readings[]` | 每个载体（`pdf`、`workbook`）的原始读数；见 §3 |
| `chosen` | 当前采用的读数；`null` 表示暂未采用 |
| `status` | `adopted` / `conflict` / `unavailable` / `confirmed` / `corrected` / `pending` |
| `corroborated` | 两个载体一致且都被采用为依据时为 `true` |
| `scope_notes[]` | 与该城市/年份匹配的脚注或范围说明（含页码与矩形） |
| `superseded[]` | 被勘误替换的旧读数及其原件位置 |
| `pending_reasons[]` | 每条待核实原因，含 `reason` 代码、说明、证据和对应核实项 id |
| `pending_review_ids[]` | 指向 `reviews[]` |
| `resolved_by` | 若该记录因勘误而解除冲突，指向勘误 id |
| `history[]` | 重新采集后与上一份交付的差异；旧交付在 `deliveries/` 中仍可打开 |
| `decision` | 当前生效的人工确认原文（若有） |

### `chosen` 字段

| 字段 | 含义 |
| --- | --- |
| `carrier` / `document` | 采用值所在载体和原件名 |
| `raw_value` | 原样文本，如 `15①`、`0`、`…`、`150000` |
| `numeric_value` | 解释后的数值；缺报/空白/不适用为 `null` |
| `unit` | 该读数自己的单位 |
| `value_status` | `value` / `zero` / `missing` / `not_published` / `not_applicable` / `unparsed` |
| `evidence_ids` | 可回查位置 |
| `citations` | 中文可读引用 |
| `basis` | 采用依据：原件读数、人工确认、或勘误替换 |
| `previous` | 勘误替换前的读数列表（保留旧载体证据） |
| `errata_id` / `overrides_decision` | 勘误替换时说明其来源与是否覆盖了旧人工确认 |

## 3. 读数（`readings[]`）

每条读数保留单一载体的解释结果，不因另一载体或勘误而被删除：

`carrier`、`document`、`record_id`、`table_id`、`column_header`、`raw_value`、
`value_text`、`numeric_value`、`value_status`、`value_status_meaning`、`unit`、
`footnote_markers`、`scope_notes`、`note`、`evidence[]`、`evidence_ids[]`、`citations[]`、
`pending_reasons[]`。

`value_status_meaning` 明确区分：

| 状态 | 含义 |
| --- | --- |
| `value` | 原件刊载数值 |
| `zero` | 原值为 0，表示零值；不是缺报 |
| `not_published` | 原值为空白，表示未刊载 |
| `not_applicable` | 破折号，表示不适用 |
| `missing` | 缺报符号（⋯/…），原件未统计 |
| `unparsed` | 文本无法解释为单一数值，保留原文 |

## 4. 证据位置

- PDF：`pdf:<文件名>@p<页码>[x0,y0,x1,y1]`。页码从 1 开始；坐标是 PDF 点，
  原点在页面左上角，顺序为左、上、右、下。
- 工作簿：`workbook:<文件名>#<工作表>!<单元格>`，合并表头覆盖范围另在
  `evidence[].header_range` 中给出（如 `C2:D2`）。

`python -m capture verify` 会按这些位置重新打开原件核对 `value_text`，
包括被采用值和工作簿合并表头下的每个叶子单元格。

## 5. 核实项（`reviews[]`）

| `kind` | 何时出现 |
| --- | --- |
| `carrier_conflict` | 同一城市/年/指标在两个载体读数或单位不一致 |
| `observation_pending` | 记录的其他待核实原因（缺单位、单位不一致、表号不一致等） |
| `errata_overrides_decision` | 勘误替换了先前人工确认的读数，需复核 |
| `errata_target_unresolved` | 勘误替换目标无法唯一对应到记录 |
| `errata_missing_unit` | 勘误替换未声明单位，未采用，未沿用旧单位 |
| `continuation_uncertain` | 续页与首页表号/指标/单位/列年份不一致，整页未生成记录 |
| `header_unresolved` | 表头无法确定观测年列 |
| `geometry_unavailable` / `geometry_unassigned` | 页内线框或文本无法定位到单元 |
| `row_unclassified` | 行无法判断是城市还是合计 |
| `workbook_unassigned` | 工作表中有单元格不在表头/数据区域解释范围内 |
| `legend_conflict` / `title_conflict` / `declaration_conflict` | 原件内部或两载体声明互相矛盾 |

核实项带 `id`、`message`、`detail`、`evidence_ids`、`observation_key`。`resolved_by`
表示该核实项已因勘误得到处理，但仍保留在清单中供复核。

## 6. 人工确认

`decisions.json` 按记录 `key` 保存：

```json
{
  "decisions": {
    "C01|2024|终端能源": {
      "action": "choose",
      "evidence_id": "pdf:Y-2025.pdf@p1[407.0,235.36,433.94,249.68]",
      "note": "按脚注①范围采用 PDF 刊值",
      "reviewer": "核验人",
      "decided_at": "2026-11-19T00:00:00+00:00"
    }
  }
}
```

规则：

1. `choose` 必须给出记录内某条读数的 `evidence_id`，采集端据此设 `chosen` 并写
   `basis: "人工确认：…"`；无法匹配证据时记录保持待核实并提示。
2. `keep_pending` 不改变数值，只记录"已看过、仍需确认"。
3. 勘误到达时，若与旧人工确认相反，正式勘误优先；旧确认原文进入
   `chosen.overrides_decision` 并生成 `errata_overrides_decision` 核实项。
4. 人工确认不会被 `refresh` 静默丢弃；变化写入 `history`，旧交付在 `deliveries/`。

## 7. 未采用但保留的原件内容

- `excluded[]`：省合计/小计行、以及因续页不一致而整页未生成记录的情况，各自带原因和位置。
- `carrier_checks[]`：每个年鉴记录的两载体声明版本、列范围、单位对照与差异。
- `notices[]`：目录与上次交付不一致等运行期提示（例如某原件从目录中消失）。
- 无法定位的表格文本：对应 `geometry_*` 核实项中的 `lines`。
