"""电子表格原件解析。

与 PDF 共用同一套表头/取值语义，但证据位置改为"表名 + 单元格地址"。
合并表头按其覆盖列展开；表尾的单位注按原件声明保留，不因为与表名
或另一载体不同就覆盖。无法归属面积外的文本进入待核实。
"""
from __future__ import annotations

import re

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

from . import headers
from .normalize import (classify_row, clean_text, merge_declared,
                        parse_declared, parse_value, scope_mentions)

CODE_SHAPE = re.compile(r'^[A-Za-z]{1,3}\d{2,}$')


def _is_year(text) -> int | None:
    from .normalize import parse_year
    return parse_year(clean_text(text))


def _merged_lookup(ws):
    lookup = {}
    for merged in ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = merged.bounds
        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                lookup[(row, col)] = (min_row, min_col, max_row, max_col)
    return lookup


def _value(ws, row, col):
    if row < 1 or col < 1:
        return ''
    return ws.cell(row=row, column=col).value


def _merged_value(ws, merged, row, col):
    """合并单元格只有左上角存值；其余位置按覆盖范围回读，绝不当作空白。"""
    bounds = merged.get((row, col))
    if bounds:
        row, col = bounds[0], bounds[1]
    return ws.cell(row=row, column=col).value


def _row_text(ws, row, max_col):
    values = []
    for col in range(1, max_col + 1):
        value = _value(ws, row, col)
        if value is not None:
            values.append(clean_text(value))
    return ' '.join(values)


def _header_rows(ws, max_row, max_col, data_rows):
    """表头行：数据行之前、不是整幅标题声明的行。"""
    first_data = min(data_rows) if data_rows else max_row + 1
    return [row for row in range(1, first_data) if not _is_title_row(ws, row, max_col)]


def _is_title_row(ws, row, max_col):
    """整行被一个跨越多列的合并区域占据、或明确是年鉴声明时，不算表头。"""
    text = clean_text(' '.join(clean_text(ws.cell(row=row, column=col).value or '')
                               for col in range(1, max_col + 1)))
    if '年鉴' in text or '勘误' in text:
        return True
    for merged in ws.merged_cells.ranges:
        min_col, min_row, max_col_of_range, max_row_of_range = merged.bounds
        if min_row <= row <= max_row_of_range and max_col_of_range - min_col + 1 >= 3:
            return True
    return False


def _column_model(ws, header_rows, max_col, merged):
    """把合并表头展开到叶子列。返回 {col: {year, indicator, header, merged}}。"""
    model = {}
    for col in range(1, max_col + 1):
        pieces, covered_by = [], []
        for row in header_rows:
            value = _merged_value(ws, merged, row, col)
            if value is None:
                continue
            text = clean_text(value)
            if not text:
                continue
            pieces.append(text)
            span = merged.get((row, col))
            if span:
                covered_by.append({'cell': ws.cell(row=span[0], column=span[1]).coordinate,
                                   'range': _range_text(span)})
        if not pieces:
            continue
        year = None
        indicator = None
        for piece in pieces:
            candidate = _is_year(piece)
            if candidate is not None:
                if year is None:
                    year = candidate
                continue
            if indicator is None:
                indicator = piece
        if year is None:
            continue
        model[col] = {'year': year, 'indicator': indicator,
                      'header': ' / '.join(pieces), 'merged': covered_by}
    return model


def _range_text(bounds):
    min_row, min_col, max_row, max_col = bounds
    start = get_column_letter(min_col) + str(min_row)
    end = get_column_letter(max_col) + str(max_row)
    return start + ':' + end if start != end else start


def parse_workbook(data: bytes, document_name: str, sheet: str | None = None) -> dict:
    import io
    workbook = load_workbook(io.BytesIO(data), data_only=True)
    results = {'document': document_name, 'sheets': [], 'reviews': [],
               'declared_items': [], 'footnotes': [], 'legend': {}}
    for ws in workbook.worksheets:
        if sheet and ws.title != sheet:
            continue
        max_row, max_col = ws.max_row, ws.max_column
        merged = _merged_lookup(ws)
        rows_text = [(row, _row_text(ws, row, max_col)) for row in range(1, max_row + 1)]
        declared = parse_declared(rows_text[0][1]) if rows_text else {}
        results['declared_items'].append(declared)

        data_rows = []
        for row in range(1, max_row + 1):
            code = clean_text(_value(ws, row, 1))
            if not CODE_SHAPE.match(code):
                continue
            if classify_row(code, clean_text(_value(ws, row, 2)))['role'] in (
                    'city', 'province_total', 'subtotal', 'unclassified'):
                data_rows.append(row)

        header_rows = _header_rows(ws, max_row, max_col, data_rows)
        column_model = _column_model(ws, header_rows, max_col, merged)

        # 表尾/表头注释：包含"单位："或符号说明的行。其余未被表头/数据解释的
        # 文本单独列出，作为可能的脚注或声明，不静默丢弃。
        unit_notes, legend_rows, other_notes = [], [], []
        header_tokens = column_model_headers(column_model) | {
            '城市代码', '城市', '终端能源'}
        for row, text in rows_text:
            if not text:
                continue
            if data_rows and min(data_rows) <= row <= max(data_rows):
                continue
            if row in header_rows:
                continue
            if '单位' in text and '：' in text:
                unit_notes.append({'row': row, 'text': text,
                                   'unit': headers.parse_unit(text)})
            if text.startswith(('符号', '注', '说明')):
                legend_rows.append({'row': row, 'text': text,
                                    'legend': headers.parse_legend(text)})
            if row != 1 and not _is_year(text) and text not in header_tokens:
                other_notes.append({'row': row, 'text': text})

        table_unit = next((note['unit'] for note in unit_notes if note['unit']), None)
        if legend_rows:
            results['legend'].update(legend_rows[0]['legend']['map'])
        table_id = headers.parse_table_id(ws.title) or headers.parse_table_id(
            rows_text[1][1] if len(rows_text) > 1 else '')

        observations, excluded = [], []
        for row in data_rows:
            code = clean_text(_value(ws, row, 1))
            name = clean_text(_value(ws, row, 2))
            role = classify_row(code, name)
            if role['role'] in ('province_total', 'subtotal'):
                excluded.append({
                    'document': document_name, 'sheet': ws.title, 'city_code': code,
                    'city_name': name, 'role': role['role'], 'reason': role['reason'],
                    'evidence': [{'carrier': 'workbook', 'document': document_name,
                                  'sheet': ws.title,
                                  'range': 'A%d:B%d' % (row, row)}],
                })
                continue
            if role['role'] != 'city':
                results['reviews'].append({
                    'kind': 'row_unclassified',
                    'message': f'工作表 {ws.title} 第 {row} 行无法判断是城市还是合计',
                    'detail': {'row': row, 'code': code, 'name': name},
                    'evidence': [{'carrier': 'workbook', 'document': document_name,
                                  'sheet': ws.title, 'range': 'A%d:B%d' % (row, row)}],
                })
                continue
            for col, column in column_model.items():
                cell = ws.cell(row=row, column=col)
                parsed_value = parse_value(cell.value, results['legend'])
                unit = table_unit
                unit_source = None
                if unit:
                    unit_source = next(n for n in unit_notes if n['unit'] == unit)
                scope_notes = [note for note in unit_notes
                               if scope_mentions(note['text'], code, name, column['year'])]
                pending = []
                if not unit:
                    pending.append('表中单位未声明')
                obs = {
                    'carrier': 'workbook',
                    'document': document_name,
                    'sheet': ws.title,
                    'table_id': table_id,
                    'indicator': column.get('indicator'),
                    'column_header': column['header'],
                    'city_code': code,
                    'city_name': name,
                    'observation_year': column['year'],
                    'unit': unit,
                    'unit_note': unit_source['text'] if unit_source else None,
                    'raw_value': parsed_value['raw_value'],
                    'value_text': parsed_value['value_text'],
                    'numeric_value': parsed_value['numeric_value'],
                    'value_status': parsed_value['value_status'],
                    'footnote_markers': parsed_value['footnote_markers'],
                    'scope_notes': scope_notes,
                    'note': parsed_value['note'],
                    'evidence': [{'carrier': 'workbook', 'document': document_name,
                                  'sheet': ws.title, 'cell': cell.coordinate,
                                  'header_range': column['merged']}],
                    'pending_reasons': pending,
                }
                observations.append(obs)

        sheets_result = {
            'sheet': ws.title,
            'state': ws.sheet_state,
            'dimensions': ws.dimensions,
            'header_rows': header_rows,
            'data_rows': data_rows,
            'columns': {str(k): v for k, v in column_model.items()},
            'unit_notes': unit_notes,
            'other_notes': other_notes,
            'observations': observations,
            'excluded': excluded,
            'declared': declared,
        }
        if column_model and sheets_result['dimensions']:
            min_col, min_row, max_col, max_row = range_boundaries(ws.dimensions)
            used = set()
            for row in data_rows:
                for col in range(1, max_col + 1):
                    used.add((row, col))
            for row in header_rows:
                for col in range(1, max_col + 1):
                    used.add((row, col))
            for note in unit_notes + legend_rows + other_notes:
                for col in range(1, max_col + 1):
                    used.add((note['row'], col))
            for row, text in rows_text:
                if text and ('年鉴' in text or '勘误' in text):
                    for col in range(1, max_col + 1):
                        used.add((row, col))
            stray = []
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    if (row, col) in used:
                        continue
                    value = ws.cell(row=row, column=col).value
                    if value not in (None, ''):
                        stray.append({'cell': ws.cell(row=row, column=col).coordinate,
                                      'value': clean_text(value)})
            if stray:
                results['reviews'].append({
                    'kind': 'workbook_unassigned',
                    'message': f'工作表 {ws.title} 有 {len(stray)} 个单元格不在表头/数据区域解释范围内',
                    'cells': stray,
                    'evidence': [{'carrier': 'workbook', 'document': document_name,
                                  'sheet': ws.title, 'cells': [s['cell'] for s in stray]}],
                })
        results['sheets'].append(sheets_result)
    results['declared'] = merge_declared(results['declared_items'])
    return results


def column_model_headers(model):
    headers_set = set()
    for column in model.values():
        headers_set.update(part.strip() for part in column['header'].split('/'))
    return headers_set
