# 城市能源年鉴跨页表格与勘误资料采集

这是围绕城市低碳公开资料的本地研发项目。`source` 提供授权的合成来源，`data` 保存
合成原始资料，`capture` 是本次实现的采集端：从正常 HTTP 入口取得 PDF 与电子表格，
按原件几何和合并表头提取城市年度读数，保留证据位置，并把无法判断的记录交给人工确认。
采集端不做趋势推断、排名或跨城市比较。

## 快速开始

使用 Python 3.11 或 3.12，先执行：

```bash
python -m pip install -r requirements.txt
python -m source.server --port 8765          # 终端一：启动来源
```

另开一个终端（运行目录默认是 `capture-run/`，只含本次采集产物）：

```bash
python -m capture fetch   --url http://127.0.0.1:8765 --run-dir capture-run
python -m capture extract --run-dir capture-run
python -m capture verify  --run-dir capture-run
```

结束后在 `capture-run/` 中得到：

| 文件 | 用途 |
| --- | --- |
| `dataset.json` | 完整采集记录：每条城市读数的所有载体读数、被采用值、证据、核实项 |
| `observations.csv` | 一行一条城市年度记录，含城市代码、观测期、指标、表中单位、原始值文本、证据位置与采用依据 |
| `reviews.md` | 人工核实清单，以及 PDF/工作簿的声明版本、列范围、单位对照 |
| `report.html` | 单文件回查页：从一条城市数据展开看到原件引用、脚注、被替换读数与待核实原因 |
| `manifest.json`、`originals/` | 目录响应、每个原件的 URL、sha256、ETag 和下载原件 |
| `decisions.json` | 人工确认记录（有确认时才生成） |
| `deliveries/` | 每次重写交付前的只读快照，用于解释"之前交付的记录" |

### 勘误到达后重新采集

来源进入勘误阶段后，重新运行同一条刷新命令即可：

```bash
python -m capture refresh --url http://127.0.0.1:8765 --run-dir capture-run
```

刷新只应用目录里明确声明的替换范围：旧年鉴的其余记录保持原样，旧交付先存档，
被替换的记录同时保留旧读数、旧人工确认和新单位。若来源不可达，刷新会失败并保留上
一次交付，不会写出半份文件。

### 人工确认与针对性检查

```bash
python -m capture review  --run-dir capture-run                 # 待核实记录与核实项
python -m capture review  --run-dir capture-run --key 'C01|2024|终端能源'
python -m capture confirm --run-dir capture-run --key 'C01|2024|终端能源' \
    --action choose --evidence 'pdf:Y-2025.pdf@p1[407.0,235.36,433.94,249.68]' \
    --note '按脚注①范围采用 PDF 刊值' --reviewer '核验人'
python -m capture confirm --run-dir capture-run --key '...' --action keep_pending \
    --note '仍需向统计部门确认'
python -m capture verify  --run-dir capture-run
```

`verify` 会回到原件重新读取每一条读数所在的 PDF 矩形/工作簿单元格，核对原始值文本，
所以它检查的是"能否回查"，而不只是产物内部自洽。命令退出码可用于流水线。

## 字段与交互契约

完整字段含义、证据位置格式、状态语义和人工确认规则见
[docs/capture-contract.md](docs/capture-contract.md)。

## 资料获取

`fetch`/`refresh` 只请求 `GET /catalog` 和目录中列出的 `/files/...`。每次请求的 URL、
字节数、sha256、ETag 都记在 `manifest.json` 的 `requests` 中。采集端不使用
`/__admin/*` 控制接口，不读取下载目录以外的路径，也不把预先整理的答案表当来源。

## 提取规则（与 docs/source-contract.md 对应）

- **出版年与观测年**：目录记录的 `publication_year` 与原件中的"年鉴 2025"只作版本声明；
  观测年只取表头年份列。测试会拒绝把 2025 当观测年。
- **合并表头**：PDF 先从矢量线框求每行带的列边界，再按叶子列展开合并表头；工作簿用
  `merged_cells` 把 `C2:D2` 的"终端能源"应用到 2023、2024 两列。只看文本行无法得到
  这个对应关系。
- **跨页续表**：续页必须与首页表号、指标、单位、观测年列完全一致才沿用列定义；
  任一不一致，该页整体不生成城市记录，并生成 `continuation_uncertain` 核实项。
- **省合计**：`P00/省合计` 按代码和名称双重判断为省级合计，写入 `excluded` 而不是城市记录；
  名称含"合计/小计"的行同样不进入城市记录。未知行进入 `row_unclassified`，不默认当城市。
- **符号**：若原件有符号说明（如 `0 零值；空白 未刊载；— 不适用；⋯ 缺报`），按说明
  解释；否则按契约默认含义。`0` 记为 `zero`，空白 `not_published`，`—` `not_applicable`，
  `⋯/…` `missing`。任何情况下 `raw_value` 保留原文。
- **脚注**：脚注上标（`①`、`[1]`）先从数值中剥离，但保留在 `footnote_markers` 和
  `raw_value` 中；与城市/年份匹配的脚注进入 `scope_notes`，例如"2024 年城甲统计范围含新区"。
- **单位**：只使用该表/该单元格自己声明的单位。某个载体缺单位时进入待核实，绝不沿用另一个
  载体的单位。
- **双载体**：PDF 与工作簿各自独立解析。声明版本、列范围、单位分别对照；同一城市/年/指标
  两载体不一致时，两条读数都以原始证据保留，记录状态为 `conflict`，并生成核实项，
  不默认电子表格更可靠。
- **勘误**：只替换勘误明确声明的表、城市、观测年（如"表一 C01 的 2024 列"），并强制采用
  勘误自己声明的单位（如 `吨标准煤`），不同旧单位混用。`150000 吨标准煤` 与
  `15 万吨标准煤` 在采用值上不做任何隐式换算。勘误若与先前人工确认相反，会采用勘误、
  保留确认原文并生成 `errata_overrides_decision` 复核项。

## 记录字段

每条 `dataset.json` 记录至少包含：

`city_code`、`city_name`、`observation_year`、`indicator`、`unit`、`raw_value`、
`numeric_value`、`value_status`、`status`、`chosen`（含 `basis`、`citations`、`evidence_ids`）、
`readings`（每个载体的原始读数与位置）、`scope_notes`、`superseded`（被勘误替换的旧读数）、
`pending_reasons` 与 `pending_review_ids`。

证据位置格式：PDF 为 `pdf:<文件名>@p<页码>[x0,y0,x1,y1]`，坐标单位是 PDF 点、原点在
左上角；工作簿为 `workbook:<文件名>#<工作表>!<单元格>`。`citations` 给出可直接核对的
中文引用。

状态含义：

- `adopted`：单一可用读数，或两载体一致且单位一致；
- `conflict`：两载体读数或单位不一致，暂不采用；
- `unavailable`：原件明确表示缺报/未刊载/不适用，没有可采用的数值；
- `confirmed`：人工从指定证据中确认采用某读数；
- `corrected`：被勘误明确替换；
- `pending`：其他需要人工确认的情形。

## 检查

```bash
python -m unittest discover -s tests -v
```

现有检查覆盖：来源真实响应与原件可读、省合计排除、续页与脚注、符号语义、双载体冲突、
勘误局部替换与人工确认留档、`verify` 能发现被篡改的读数、交付文件生成。测试直接读取
`data/` 中的真实 PDF 与工作簿，不使用预整理答案表。

## 容器

```bash
docker build -t yearbook-table-capture .
docker run --rm -p 8765:8765 yearbook-table-capture
docker run --rm yearbook-table-capture python -m unittest discover -s tests -v
```

容器内采集端可作为独立进程运行，例如：
`docker run --rm --network host -v "$PWD/capture-run:/app/capture-run" yearbook-table-capture python -m capture fetch --url http://127.0.0.1:8765 --run-dir /app/capture-run`。

不要将运行日志、凭据或 `capture-run/` 下的采集数据提交到版本库。
