"""勘误原件解析：只取明确声明的替换范围、替换单元与新单位。

勘误不是"新版本整本年鉴"，也不是"只替换数值"：

* 范围（表、城市、列/观测年）按原件声明，未声明的单元格不撤回；
* 替换单元必须带自己的单位；没有单位就不沿用旧单位；
* PDF 与工作簿两个载体各自解析，互相核对；不一致时双方原样保留。
"""
from __future__ import annotations

import io
import re

import fitz
from openpyxl import load_workbook

from . import headers
from .normalize import clean_text, parse_declared

SCOPE_RE = re.compile(r'替换范围\s*[:：]\s*(.*)')
UNIT_RE = re.compile(r'(?:更正)?单位\s*[:：]\s*([^，,；;。\n]+)')
CITY_RE = re.compile(r'\b([CP]\d{2,})\b')
YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
ROW_RE = re.compile(r'^([A-Za-z]{1,3}\d{2,})\s+(\S+)\s+((?:19|20)\d{2})\s+([^\s；;]+)')


def parse_errata_pdf(data: bytes, document_name: str) -> dict:
    with fitz.open(stream=data, filetype='pdf') as doc:
        lines = []
        for page_index, page in enumerate(doc):
            for block in page.get_text('dict')['blocks']:
                if block.get('type') != 0:
                    continue
                for line in block['lines']:
                    text = clean_text(''.join(span['text'] for span in line['spans']))
                    if text:
                        lines.append({'text': text, 'page': page_index + 1,
                                      'bbox': list(line['bbox'])})
    declared_text = next((line['text'] for line in lines
                          if '勘误' in line['text'] or '年鉴' in line['text']), None)
    declared = parse_declared(declared_text or '')
    scope_text = unit_text = None
    rows = []
    for line in lines:
        text = line['text']
        match = SCOPE_RE.search(text)
        if match:
            scope_text = text
            continue
        match = UNIT_RE.search(text)
        if match:
            unit_text = text
            continue
        match = ROW_RE.match(text)
        if match:
            rows.append({
                'city_code': match.group(1),
                'city_name': match.group(2),
                'observation_year': int(match.group(3)),
                'raw_value': match.group(4),
                'evidence': {'carrier': 'pdf', 'document': document_name,
                             'page': line['page'], 'rect': line['bbox']},
            })
    declared_scope = _declared_scope(lines)
    scope = parse_scope(scope_text or '')
    if declared_scope.get('table_id') and not scope.get('table_id'):
        scope['table_id'] = declared_scope['table_id']
        scope['table_source'] = 'replacement_rows'
    if not scope.get('cities'):
        scope['cities'] = sorted({row['city_code'] for row in rows})
    if not scope.get('years'):
        scope['years'] = sorted({row['observation_year'] for row in rows})
    unit = parse_unit_statement(unit_text)
    return {
        'document': document_name,
        'declared': declared,
        'scope': scope,
        'unit': unit,
        'rows': rows,
        'statements': {'scope': scope_text, 'unit': unit_text,
                       'declared': declared_text,
                       'rows': [line['text'] for line in lines
                                if ROW_RE.match(line['text'])]},
    }


def _declared_scope(lines: list[dict]) -> dict:
    """没有"替换范围："句时，从标题里的表号与替换行反推范围。"""
    table_id = None
    for line in lines:
        table_id = table_id or headers.parse_table_id(line['text'])
        if table_id:
            break
    return {'table_id': table_id}


def parse_scope(text: str) -> dict:
    text = clean_text(text)
    table_id = headers.parse_table_id(text)
    cities = CITY_RE.findall(text)
    years = [int(match.group(1)) for match in YEAR_RE.finditer(text)]
    whole = bool(re.search(r'其他(列|城市|单元)?(不变|不替换|保持)', text))
    return {
        'table_id': table_id,
        'cities': sorted(set(cities)),
        'years': sorted(set(years)),
        'statement': text or None,
        'partial': whole or bool(cities or years),
    }


def parse_unit_statement(text: str | None) -> str | None:
    if not text:
        return None
    match = UNIT_RE.search(clean_text(text))
    if not match:
        return None
    return clean_text(match.group(1)) or None


def parse_errata_workbook(data: bytes, document_name: str) -> dict:
    workbook = load_workbook(io.BytesIO(data), data_only=True)
    ws = workbook.worksheets[0]
    max_row, max_col = ws.max_row, ws.max_column
    merged = {}
    for merged_range in ws.merged_cells.ranges:
        min_col, min_row, max_col_of_range, max_row_of_range = merged_range.bounds
        for row in range(min_row, max_row_of_range + 1):
            for col in range(min_col, max_col_of_range + 1):
                merged[(row, col)] = (min_row, min_col)

    title = clean_text(ws.cell(row=1, column=1).value)
    declared = parse_declared(title)

    header_rows = [row for row in range(1, max_row + 1)
                   if any(clean_text(ws.cell(row=row, column=col).value) in ('城市代码', '城市')
                          for col in range(1, max_col + 1))]
    first_header = min(header_rows) if header_rows else 1
    data_rows = []
    for row in range(first_header + 1, max_row + 1):
        code = clean_text(ws.cell(row=row, column=1).value)
        if re.fullmatch(r'[A-Za-z]{1,3}\d{2,}', code):
            data_rows.append(row)

    columns = {}
    for col in range(1, max_col + 1):
        pieces = []
        for row in range(first_header, (min(data_rows) if data_rows else max_row + 1)):
            cell_row, cell_col = merged.get((row, col), (row, col))
            text = clean_text(ws.cell(row=cell_row, column=cell_col).value)
            if text:
                pieces.append(text)
        year = next((int(m.group(1)) for piece in pieces
                     for m in [YEAR_RE.fullmatch(piece)] if m), None)
        indicator = next((piece for piece in pieces if not YEAR_RE.fullmatch(piece)), None)
        if year is not None:
            columns[col] = {'year': year, 'indicator': indicator,
                            'header': ' / '.join(pieces)}

    notes = []
    for row in range(1, max_row + 1):
        if data_rows and min(data_rows) <= row <= max(data_rows):
            continue
        text = clean_text(' '.join(clean_text(ws.cell(row=row, column=col).value or '')
                                   for col in range(1, max_col + 1)))
        if text and ('单位' in text or '替换' in text or '勘误' in text):
            notes.append({'row': row, 'text': text})

    unit_note = next((note for note in notes if UNIT_RE.search(note['text'])), None)
    scope_note = next((note for note in notes if '替换' in note['text']), None)
    unit = parse_unit_statement(unit_note['text']) if unit_note else None
    scope = parse_scope(scope_note['text'] if scope_note else
                        ' '.join(note['text'] for note in notes if '替换' in note['text']))
    if not scope.get('table_id'):
        # 工作表名就是勘误的目标表（此处为"表一"）。
        scope['table_id'] = headers.parse_table_id(ws.title)
        scope['table_source'] = 'sheet_name'

    rows = []
    for row in data_rows:
        code = clean_text(ws.cell(row=row, column=1).value)
        name = clean_text(ws.cell(row=row, column=2).value)
        for col, column in columns.items():
            cell = ws.cell(row=row, column=col)
            if cell.value in (None, ''):
                # 勘误表中空着的列不在替换范围，不能解释为"未刊载"。
                continue
            rows.append({
                'city_code': code,
                'city_name': name,
                'observation_year': column['year'],
                'raw_value': clean_text(cell.value),
                'evidence': {'carrier': 'workbook', 'document': document_name,
                             'sheet': ws.title, 'cell': cell.coordinate},
            })
    return {
        'document': document_name,
        'declared': declared,
        'scope': scope,
        'unit': unit,
        'rows': rows,
        'statements': {'scope': scope_note['text'] if scope_note else None,
                       'unit': unit_note['text'] if unit_note else None,
                       'declared': title,
                       'rows': [clean_text(ws.cell(row=row, column=col).value or '')
                                for row in data_rows for col in columns]},
        'sheet': ws.title,
    }


def consolidate(carriers: list[dict], record_id: str, corrects: str) -> dict:
    """合并勘误的两个载体，保持双方原文并暴露差异。"""
    scope = next((c['scope'] for c in carriers if c['scope'].get('statement')), {})
    unit = next((c['unit'] for c in carriers if c.get('unit')), None)
    warnings = []
    for scope_item in carriers:
        if scope.get('table_id') and scope_item['scope'].get('table_id') \
                and scope['table_id'] != scope_item['scope']['table_id']:
            warnings.append({'kind': 'errata_scope_conflict', 'field': 'table_id',
                             'values': [scope['table_id'], scope_item['scope']['table_id']]})
        if unit and scope_item.get('unit') and unit != scope_item['unit']:
            warnings.append({'kind': 'errata_unit_conflict', 'field': 'unit',
                             'values': [unit, scope_item['unit']]})
    by_key = {}
    for carrier in carriers:
        for row in carrier['rows']:
            key = (row['city_code'], row['observation_year'])
            item = dict(row)
            item['unit'] = carrier.get('unit')
            item['carrier'] = row['evidence']['carrier']
            by_key.setdefault(key, []).append(item)
    replacements, conflicts = [], []
    for key, items in by_key.items():
        values = {(item.get('raw_value'), item.get('unit')) for item in items}
        if len(values) > 1:
            conflicts.append({'kind': 'errata_replacement_conflict', 'key': key,
                              'readings': items})
        replacements.extend(items)
    return {
        'id': record_id,
        'corrects': corrects,
        'table_id': scope.get('table_id'),
        'scope': scope,
        'unit': unit,
        'replacements': replacements,
        'conflicts': conflicts,
        'warnings': warnings,
        'carriers': carriers,
    }


def parse_errata(manifest: dict, destination) -> list[dict]:
    from .model import _resolve
    errata = []
    for record in manifest['records']:
        if not record.get('corrects'):
            continue
        carriers = []
        for carrier in ('pdf', 'workbook'):
            spec = record.get('carriers', {}).get(carrier)
            if not spec:
                continue
            path = _resolve(manifest, spec)
            if carrier == 'pdf':
                carriers.append(parse_errata_pdf(path.read_bytes(), path.name))
            else:
                carriers.append(parse_errata_workbook(path.read_bytes(), path.name))
        errata.append(consolidate(carriers, record['id'], record['corrects']))
    return errata
