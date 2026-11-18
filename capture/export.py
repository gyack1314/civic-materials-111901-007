"""交付物导出：CSV、人工核实清单、单文件 HTML 回查页。"""
from __future__ import annotations

import csv
import html
from pathlib import Path

CSV_COLUMNS = [
    ('city_code', '城市代码'),
    ('city_name', '城市'),
    ('observation_year', '观测年'),
    ('indicator', '指标'),
    ('unit', '表中单位'),
    ('raw_value', '原始值文本'),
    ('numeric_value', '解释数值'),
    ('value_status', '值状态'),
    ('status', '记录状态'),
    ('value_status_meaning', '状态含义'),
    ('basis', '采用依据'),
    ('evidence_ids', '证据位置'),
    ('citations', '原件引用'),
    ('pending_reasons', '待核实原因'),
    ('scope_notes', '范围/脚注'),
    ('carrier', '采用载体'),
    ('document', '采用原件'),
]


def _row(record: dict) -> dict:
    chosen = record.get('chosen') or {}
    return {
        'city_code': record.get('city_code'),
        'city_name': record.get('city_name'),
        'observation_year': record.get('observation_year'),
        'indicator': record.get('indicator'),
        'unit': chosen.get('unit') if chosen else ','.join(record.get('units') or []),
        'raw_value': chosen.get('raw_value') if chosen else None,
        'numeric_value': chosen.get('numeric_value') if chosen else None,
        'value_status': chosen.get('value_status') if chosen else record.get('value_status'),
        'status': record.get('status'),
        'value_status_meaning': record.get('status_meaning'),
        'basis': chosen.get('basis') if chosen else None,
        'evidence_ids': ' '.join(chosen.get('evidence_ids') or []) if chosen else '',
        'citations': ' | '.join(chosen.get('citations') or []) if chosen else '',
        'pending_reasons': '；'.join(reason['message'] for reason in
                                  record.get('pending_reasons') or []),
        'scope_notes': '；'.join(
            f"{note.get('marker', '')}{note.get('text', '')}".strip()
            for note in record.get('scope_notes') or []),
        'carrier': chosen.get('carrier') if chosen else None,
        'document': chosen.get('document') if chosen else None,
    }


def write_csv(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = {key: label for key, label in CSV_COLUMNS}
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[label for _, label in CSV_COLUMNS])
        writer.writeheader()
        for record in records:
            row = _row(record)
            writer.writerow({labels[key]: row[key] for key in labels})


def write_review_markdown(path: Path, dataset: dict) -> None:
    lines = ['# 人工核实清单', '',
             f"生成时间：{dataset.get('generated_at')}；"
             f"记录 {len(dataset['records'])} 条，核实项 {len(dataset['reviews'])} 条。", '']
    if dataset.get('carrier_checks'):
        lines.extend(['## 载体核对', ''])
        for check in dataset['carrier_checks']:
            if check['kind'] == 'declaration':
                lines.append(f"- {check['record']} 声明版本：冲突 {len(check['conflicts'])} 处")
                for conflict in check['conflicts']:
                    lines.append(f"  - {conflict['field']}: PDF={conflict['pdf']!r}，"
                                 f"工作簿={conflict['workbook']!r}")
            elif check['kind'] == 'columns':
                diff = check['diff']
                lines.append(f"- {check['record']} 列范围：共同 {len(diff['common'])}，"
                             f"仅 PDF {diff['only_pdf']}，仅工作簿 {diff['only_workbook']}")
            elif check['kind'] == 'unit':
                lines.append(f"- {check['record']} 单位声明：PDF={check['pdf']}，"
                             f"工作簿={check['workbook']}")
        lines.append('')

    pending = [record for record in dataset['records']
               if record['status'] in ('pending', 'conflict', 'unavailable')]
    lines.extend(['## 需要人工确认的城市记录', ''])
    if not pending:
        lines.append('（无）')
    for record in pending:
        lines.append(f"### {record['city_code']} {record.get('city_name') or ''} "
                     f"{record['observation_year']} {record['indicator']}")
        lines.append(f"- 记录状态：{record['status']}")
        for reason in record.get('pending_reasons') or []:
            lines.append(f"- {reason['message']}"
                         + (f"（核实项 {reason['review_id']}）" if reason.get('review_id') else ''))
        for item in record.get('readings') or []:
            lines.append(f"- {item['carrier']}：原值 {item['raw_value']!r}，"
                         f"数值 {item['numeric_value']!r}，单位 {item['unit']!r}；"
                         + '；'.join(item.get('citations') or []))
        lines.append('')

    lines.extend(['## 全部核实项', ''])
    for review in dataset['reviews']:
        lines.append(f"- `{review['id']}` [{review['kind']}] {review['message']}")
    lines.append('')
    lines.extend(['## 勘误应用范围', ''])
    for document in dataset['errata']:
        lines.append(f"- {document['id']} → {document['corrects']}；"
                     f"范围 {document['scope']}；单位 {document['unit']!r}")
        for target in document['targets']:
            lines.append(f"  - 已应用：{target.get('applied')} {target.get('replacement', {}).get('city_code', '')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _esc(value) -> str:
    return html.escape('' if value is None else str(value))


def write_html(path: Path, dataset: dict) -> None:
    """单文件回查页：从一条城市数据能看到所有载体读数、引用和核实项。"""
    reviews = {review['id']: review for review in dataset['reviews']}
    cards = []
    for record in dataset['records']:
        chosen = record.get('chosen') or {}
        status_class = record['status']
        readings = []
        for item in record.get('readings') or []:
            readings.append(
                '<tr><td>{carrier}</td><td>{raw}</td><td>{num}</td><td>{unit}</td>'
                '<td>{status}</td><td>{cite}</td></tr>'.format(
                    carrier=_esc(item.get('carrier')),
                    raw=_esc(item.get('raw_value')),
                    num=_esc(item.get('numeric_value')),
                    unit=_esc(item.get('unit')),
                    status=_esc(item.get('value_status_meaning') or item.get('note')),
                    cite='<br>'.join(_esc(c) for c in item.get('citations') or []),
                ))
        superseded = []
        for item in record.get('superseded') or []:
            reading = item['reading']
            superseded.append(
                f"<li>旧读数 {_esc(reading.get('raw_value'))} "
                f"{_esc(reading.get('unit'))}（{_esc(item['reason'])}）</li>")
        pending = []
        for reason in record.get('pending_reasons') or []:
            pending.append(
                f"<li>{_esc(reason['message'])}"
                + (f" <code>{_esc(reason['review_id'])}</code>"
                   if reason.get('review_id') else '')
                + '</li>')
        scope = ''.join(
            f"<li>{_esc(note.get('marker', ''))}{_esc(note.get('text', ''))}</li>"
            for note in record.get('scope_notes') or [])
        cards.append(f"""
<article class="card {status_class}" id="{_esc(record['key'])}">
  <h2>{_esc(record['city_code'])} {_esc(record.get('city_name'))}
      <span>{_esc(record['observation_year'])} · {_esc(record['indicator'])}</span></h2>
  <p class="summary">状态 <strong>{_esc(record['status'])}</strong>
    · 采用值 <strong>{_esc(chosen.get('raw_value') if chosen else record.get('value_status'))}</strong>
    · 单位 <strong>{_esc(chosen.get('unit') if chosen else '—')}</strong>
    · {_esc(record.get('status_meaning'))}</p>
  <p class="basis">采用依据：{_esc(chosen.get('basis') if chosen else '暂未采用')}</p>
  <table><thead><tr><th>载体</th><th>原始值文本</th><th>解释数值</th><th>单位</th>
    <th>值状态</th><th>原件位置</th></tr></thead><tbody>{''.join(readings)}</tbody></table>
  {('<p>被替换读数：</p><ul>' + ''.join(superseded) + '</ul>') if superseded else ''}
  {('<p>统计范围/脚注：</p><ul>' + scope + '</ul>') if scope else ''}
  {('<p>待核实：</p><ul>' + ''.join(pending) + '</ul>') if pending else ''}
</article>""")
    review_items = ''.join(
        f"<li><code>{_esc(review['id'])}</code> [{_esc(review['kind'])}] "
        f"{_esc(review['message'])}</li>" for review in dataset['reviews'])
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>城市能源年鉴采集回查页</title>
<style>
body{{font:15px/1.55 system-ui,'Noto Sans CJK SC',sans-serif;margin:0;padding:2rem;background:#f6f7f9;color:#1d242c}}
h1{{font-size:1.5rem}} .grid{{display:grid;gap:1rem}}
.card{{background:#fff;border:1px solid #dce1e7;border-left:6px solid #3b7f5c;border-radius:8px;padding:1rem 1.2rem}}
.card.pending,.card.conflict,.card.unavailable{{border-left-color:#b06a17}}
.card h2{{font-size:1.05rem;margin:.1rem 0 .5rem}}
.card h2 span{{font-weight:400;color:#5a6570;font-size:.9rem;margin-left:.5rem}}
.summary strong{{color:#0f4c81}}
table{{width:100%;border-collapse:collapse;margin:.6rem 0;font-size:.9rem}}
th,td{{border-bottom:1px solid #e5e9ee;padding:.35rem .45rem;text-align:left;vertical-align:top}}
th{{background:#f0f3f6}} code{{background:#eef1f4;padding:0 .25rem;border-radius:3px}}
.meta{{color:#5a6570;font-size:.85rem}}
</style></head><body>
<h1>城市能源年鉴采集回查页</h1>
<p class="meta">生成时间 {_esc(dataset.get('generated_at'))} · 记录 {len(dataset['records'])} 条 ·
核实项 {len(dataset['reviews'])} 条 · 来源于本地合成资料</p>
<section><h2>核实项</h2><ul>{review_items or '<li>（无）</li>'}</ul></section>
<div class="grid">{''.join(cards)}</div>
</body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding='utf-8')
