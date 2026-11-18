"""PDF 几何表格解析。

不做"按文本行切分"的推断。每一页先由矢量线框找出横线和竖线，
按行带重新计算该行带内实际存在的列边界，因此合并表头会自然获得
它覆盖的多个叶子列；跨页续表则先校验表号、指标和列年份一致，
不一致的记录进入待核实。
"""
from __future__ import annotations

import re
from collections import defaultdict

import fitz

from . import headers
from .normalize import (CODE_RE, classify_row, clean_text, parse_declared,
                        parse_value, scope_mentions)

TOL = 0.8


def _line_segments(page):
    horizontal, vertical = [], []
    for drawing in page.get_drawings():
        for item in drawing.get('items', []):
            if item[0] == 'l':
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) <= TOL:
                    horizontal.append((round((p1.y + p2.y) / 2, 2), min(p1.x, p2.x), max(p1.x, p2.x)))
                elif abs(p1.x - p2.x) <= TOL:
                    vertical.append((round((p1.x + p2.x) / 2, 2), min(p1.y, p2.y), max(p1.y, p2.y)))
            elif item[0] == 're':
                rect = item[1]
                horizontal.extend([
                    (round(rect.y0, 2), rect.x0, rect.x1),
                    (round(rect.y1, 2), rect.x0, rect.x1),
                ])
                vertical.extend([
                    (round(rect.x0, 2), rect.y0, rect.y1),
                    (round(rect.x1, 2), rect.y0, rect.y1),
                ])
    return horizontal, vertical


def _unique(values, tol=TOL):
    result = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tol:
            result.append(value)
    return result


def _lines(page):
    lines = []
    for block in page.get_text('dict')['blocks']:
        if block.get('type') != 0:
            continue
        for line in block['lines']:
            text = clean_text(''.join(span['text'] for span in line['spans']))
            if not text:
                continue
            blocks = []
            for span in line['spans']:
                span_text = clean_text(span['text'])
                if span_text:
                    blocks.append({'text': span_text, 'bbox': list(span['bbox']),
                                   'size': span['size'], 'font': span['font']})
            lines.append({'text': text, 'bbox': list(line['bbox']),
                          'spans': blocks, 'dir': line['dir']})
    return lines


def _row_bands(horizontal, top, bottom):
    ys = _unique([y for y, x0, x1 in horizontal if top <= y <= bottom])
    if len(ys) < 2:
        return [], ys
    return [(ys[i], ys[i + 1]) for i in range(len(ys) - 1)], ys


def _columns_for_band(vertical, band):
    y0, y1 = band
    mid = (y0 + y1) / 2
    xs = _unique([x for x, sy0, sy1 in vertical if sy0 - 1 <= mid <= sy1 + 1])
    return xs


def _leaf_columns(vertical, bands):
    xs = set()
    for band in bands:
        xs.update(_columns_for_band(vertical, band))
    xs = _unique(xs)
    return [{'index': i, 'x0': xs[i], 'x1': xs[i + 1]} for i in range(len(xs) - 1)]


def _leaf_span(leafs, x0, x1):
    contained = [leaf['index'] for leaf in leafs
                 if leaf['x0'] >= x0 - TOL and leaf['x1'] <= x1 + TOL]
    if contained:
        return contained
    return [leaf['index'] for leaf in leafs
            if min(leaf['x1'], x1) - max(leaf['x0'], x0) > 1]


def _overlaps(band, bbox):
    mid = (bbox[1] + bbox[3]) / 2
    return band[0] - 1 <= mid <= band[1] + 1


def _assign_lines(lines, bands, vertical, leafs):
    """把文本行放进 (行带, 叶子列区间)。返回 rows 与未定位行。"""
    rows = defaultdict(lambda: defaultdict(lambda: {'texts': [], 'bboxes': [], 'spans': []}))
    unassigned = []
    for line in lines:
        matched = [i for i, band in enumerate(bands) if _overlaps(band, line['bbox'])]
        if not matched:
            unassigned.append({'text': line['text'], 'bbox': line['bbox'], 'reason': '不在任何表格行带内'})
            continue
        band_index = matched[0]
        xs = _columns_for_band(vertical, bands[band_index])
        if len(xs) < 2:
            unassigned.append({'text': line['text'], 'bbox': line['bbox'], 'reason': '该行带没有列线'})
            continue
        center = (line['bbox'][0] + line['bbox'][2]) / 2
        cell_start = None
        for i in range(len(xs) - 1):
            if xs[i] - TOL <= center <= xs[i + 1] + TOL:
                cell_start = i
                break
        if cell_start is None:
            unassigned.append({'text': line['text'], 'bbox': line['bbox'], 'reason': '文本中心在列线之外'})
            continue
        span = _leaf_span(leafs, xs[cell_start], xs[cell_start + 1])
        if not span:
            unassigned.append({'text': line['text'], 'bbox': line['bbox'], 'reason': '无法映射到叶子列'})
            continue
        key = (span[0], span[-1])
        cell = rows[band_index][key]
        cell['texts'].append(line['text'])
        cell['bboxes'].append(line['bbox'])
        cell['spans'].extend(line['spans'])
        cell['bbox'] = [min(cell['bbox'][0], line['bbox'][0]) if 'bbox' in cell else line['bbox'][0],
                        min(cell['bbox'][1], line['bbox'][1]) if 'bbox' in cell else line['bbox'][1],
                        max(cell['bbox'][2], line['bbox'][2]) if 'bbox' in cell else line['bbox'][2],
                        max(cell['bbox'][3], line['bbox'][3]) if 'bbox' in cell else line['bbox'][3]]
    return rows, unassigned


def _cell_text(cell):
    return clean_text(' '.join(cell['texts']))


def _table_blocks(page):
    """取页面顶部的标题/声明块与页脚注释块。"""
    blocks = []
    for block in page.get_text('dict')['blocks']:
        if block.get('type') != 0:
            continue
        text = clean_text(' '.join(
            span['text'] for line in block['lines'] for span in line['spans']))
        if text:
            blocks.append({'text': text, 'bbox': list(block['bbox'])})
    return blocks


def _merge_legend(legend_maps):
    merged, conflicts = {}, []
    for source in legend_maps:
        for symbol, meaning in source['map'].items():
            if symbol in merged and merged[symbol] != meaning:
                conflicts.append({'symbol': symbol, 'values': [merged[symbol], meaning],
                                  'texts': [source['text']]})
            merged.setdefault(symbol, meaning)
    return merged, conflicts


def _parse_footnotes(blocks):
    result = []
    for block in blocks:
        text = clean_text(block['text'])
        if not text or text[0] not in '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳':
            continue
        marker, note = text[0], text[1:].strip()
        result.append({'marker': marker, 'text': note, 'bbox': block['bbox']})
    return result


def parse_pdf(data: bytes, document_name: str) -> dict:
    declared_items, legend_maps, footnotes = [], [], []
    page_results, reviews = [], []
    with fitz.open(stream=data, filetype='pdf') as doc:
        for page_index, page in enumerate(doc):
            page_no = page_index + 1
            blocks = _table_blocks(page)
            headings = headers.extract_headings(
                [dict(block, page=page_no) for block in blocks])
            for line in headings['declared_lines']:
                declared_items.append(parse_declared(line['text']))
            for block in headings['legend_blocks']:
                parsed = headers.parse_legend(block['text'])
                parsed['page'] = page_no
                parsed['bbox'] = block['bbox']
                legend_maps.append(parsed)
            footnotes.extend(_parse_footnotes(headings['footnote_blocks']))

            horizontal, vertical = _line_segments(page)
            bands, _ = _row_bands(horizontal, 0, page.rect.height)
            leafs = _leaf_columns(vertical, bands)
            lines = _lines(page)
            if len(lines) >= 2 and (len(bands) < 2 or len(leafs) < 2):
                reviews.append({
                    'kind': 'geometry_unavailable',
                    'message': f'PDF 第 {page_no} 页未能建立行列几何，整页文本进入人工核实',
                    'page': page_no,
                    'lines': [{'text': line['text'], 'bbox': line['bbox']} for line in lines],
                    'evidence': [{'carrier': 'pdf', 'document': document_name, 'page': page_no,
                                  'rect': [0, 0, round(page.rect.width, 2), round(page.rect.height, 2)]}],
                })
                page_results.append({'page': page_no, 'rows': [], 'table': None, 'unassigned': []})
                continue
            if len(bands) < 2 or len(leafs) < 2:
                page_results.append({'page': page_no, 'rows': [], 'table': None, 'unassigned': []})
                continue

            rows, unassigned = _assign_lines(lines, bands, vertical, leafs)
            title = headers.merge_titles([line for line in headings['title_lines']
                                          if line['page'] == page_no])
            if title and title.get('conflict'):
                reviews.append({
                    'kind': 'title_conflict',
                    'message': f'PDF 第 {page_no} 页出现多个表号，无法确定所属表',
                    'detail': title,
                    'evidence': [{'carrier': 'pdf', 'document': document_name, 'page': page_no,
                                  'rect': [0, 0, round(page.rect.width, 2), round(page.rect.height, 2)]}],
                })
            # 标题行、表名行、页脚注释本身不是数据单元，已由标题/符号说明流程解释；
            # 只有真正无法解释的正文行才进入待核实。
            explained = [line['text'] for line in headings['title_lines']]
            explained += [line['text'] for line in headings['declared_lines']]
            explained += [block['text'] for block in headings['legend_blocks']]
            explained += [block['text'] for block in headings['footnote_blocks']]
            unexplained = [line for line in unassigned if line['text'] not in explained]
            if unexplained:
                reviews.append({
                    'kind': 'geometry_unassigned',
                    'message': f'PDF 第 {page_no} 页有 {len(unexplained)} 行文本无法定位到表格单元',
                    'page': page_no,
                    'lines': unexplained,
                    'evidence': [{'carrier': 'pdf', 'document': document_name, 'page': page_no,
                                  'rect': [0, 0, round(page.rect.width, 2), round(page.rect.height, 2)]}],
                })
            page_results.append({'page': page_no, 'rows': rows, 'table': title,
                                 'unassigned': unexplained,
                                 'bands': bands, 'leafs': leafs})
    merged_legend, legend_conflicts = _merge_legend(legend_maps)
    declared = _merge_declared(declared_items)
    for conflict in legend_conflicts:
        reviews.append({'kind': 'legend_conflict', 'message': '符号说明前后不一致',
                        'detail': conflict})
    return {
        'document': document_name,
        'declared': declared,
        'legend': merged_legend,
        'legend_texts': [item['text'] for item in legend_maps],
        'footnotes': footnotes,
        'pages': page_results,
        'reviews': reviews,
    }


def _merge_declared(items):
    from .normalize import merge_declared
    return merge_declared(items)


def build_page_tables(parsed: dict) -> dict:
    """把逐页几何结果整理成表、列模型、观测值。

    跨页续表必须与上一页的表号、指标和列年份完全一致；否则该页
    所有行进入待核实，不沿用上一页的年份。
    """
    observations, excluded, reviews = [], [], []
    tables: dict[str, dict] = {}
    previous = None
    for page in parsed['pages']:
        title = page.get('table')
        if not title or not title.get('table_id'):
            continue
        page_no = page['page']
        table_id = title['table_id']
        page_columns = _column_model(page)

        if not title.get('continuation'):
            table = tables.setdefault(table_id, {
                'table_id': table_id, 'indicator': title.get('indicator'),
                'unit': title.get('unit'), 'pages': [], 'columns': {},
                'continuation_ok': True,
            })
            table['pages'].append(page_no)
            previous = table
        else:
            if previous is None or previous['table_id'] != table_id:
                reviews.append({
                    'kind': 'continuation_uncertain',
                    'message': f'PDF 第 {page_no} 页标注续表，但前一页表号不匹配，无法判断续接关系；'
                               f'该页不生成城市记录',
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                                  'rect': title['bbox']}],
                })
                previous = None
                excluded.extend({
                    'document': parsed['document'], 'page': page_no, 'city_code': None,
                    'city_name': None, 'role': 'unresolved_continuation',
                    'reason': '续表关系无法判断，整页待核实',
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'],
                                  'page': page_no, 'rect': title['bbox']}],
                } for _ in [0])
                continue
            table = previous
            table['pages'].append(page_no)
            mismatch = []
            if title.get('indicator') and title['indicator'] != table.get('indicator'):
                mismatch.append('指标')
            if title.get('unit') and table.get('unit') and title['unit'] != table['unit']:
                mismatch.append('单位')
            if mismatch:
                reviews.append({
                    'kind': 'continuation_uncertain',
                    'message': f'PDF 第 {page_no} 页续表与首页的' + '、'.join(mismatch)
                               + '不一致，不能沿用首页定义，该页不生成城市记录',
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                                  'rect': title['bbox']}],
                })
                table['continuation_ok'] = False
                excluded.append({
                    'document': parsed['document'], 'page': page_no, 'city_code': None,
                    'city_name': None, 'role': 'continuation_mismatch',
                    'reason': '续表声明与首页不一致，整页待核实',
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'],
                                  'page': page_no, 'rect': title['bbox']}],
                })
                continue

        # 列模型：首页建立；续页必须与首页完全一致才沿用，否则该页不生成记录。
        if not table['columns']:
            table['columns'] = page_columns
            table['header_bands'] = _header_bands(page)
            table.setdefault('title_evidence', title.get('sources') or [])
        elif page_columns != table['columns']:
            reviews.append({
                'kind': 'continuation_uncertain',
                'message': f'PDF 第 {page_no} 页续表的列年份或指标与首页不一致，'
                           f'该页不沿用首页列定义，不生成城市记录',
                'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                              'rect': title['bbox']}],
                'detail': {'expected': table['columns'], 'actual': page_columns},
            })
            excluded.append({
                'document': parsed['document'], 'page': page_no, 'city_code': None,
                'city_name': None, 'role': 'column_mismatch',
                'reason': '续表列定义与首页不一致，整页待核实',
                'evidence': [{'carrier': 'pdf', 'document': parsed['document'],
                              'page': page_no, 'rect': title['bbox']}],
            })
            continue

        if not table['columns']:
            reviews.append({
                'kind': 'header_unresolved',
                'message': f'PDF 第 {page_no} 页表头无法确定观测年列，整页不生成城市记录',
                'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                              'rect': title['bbox']}],
            })
            continue

        code_col, name_col = _label_columns(page)
        data_bands = _data_bands(page, code_col)
        for band_index in data_bands:
            cells = page['rows'].get(band_index, {})
            if not cells:
                continue
            code_cell = cells.get((code_col, code_col))
            name_cell = cells.get((name_col, name_col))
            code = _cell_text(code_cell) if code_cell else ''
            name = _cell_text(name_cell) if name_cell else ''
            role = classify_row(code, name)
            if role['role'] == 'blank':
                continue
            row_bbox = _row_bbox(cells)
            if role['role'] in ('province_total', 'subtotal'):
                excluded.append({
                    'document': parsed['document'], 'page': page_no, 'city_code': code,
                    'city_name': name, 'role': role['role'], 'reason': role['reason'],
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                                  'rect': row_bbox}],
                })
                continue
            if role['role'] != 'city':
                reviews.append({
                    'kind': 'row_unclassified',
                    'message': f'PDF 第 {page_no} 页行 {code or name!r} 无法判断是城市还是合计',
                    'evidence': [{'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                                  'rect': row_bbox}],
                    'row_text': _row_text(cells),
                })
                continue
            for leaf_col, column in table['columns'].items():
                if not isinstance(leaf_col, int):
                    continue
                cell = cells.get((leaf_col, leaf_col))
                raw_text = _cell_text(cell) if cell else ''
                status_reasons = []
                if not table.get('unit'):
                    status_reasons.append('表中单位缺失')
                parsed_value = parse_value(raw_text, parsed.get('legend'))
                note_details = []
                for footnote in parsed.get('footnotes', []):
                    if footnote['marker'] in parsed_value['footnote_markers']:
                        note_details.append(footnote)
                scope_notes = []
                for footnote in note_details:
                    if scope_mentions(footnote['text'], code, name, column.get('year')):
                        scope_notes.append(footnote)
                observation = {
                    'carrier': 'pdf',
                    'document': parsed['document'],
                    'table_id': table_id,
                    'indicator': column.get('indicator'),
                    'column_header': column.get('header'),
                    'city_code': code,
                    'city_name': name,
                    'observation_year': column.get('year'),
                    'unit': table.get('unit'),
                    'raw_value': parsed_value['raw_value'],
                    'value_text': parsed_value['value_text'],
                    'numeric_value': parsed_value['numeric_value'],
                    'value_status': parsed_value['value_status'],
                    'footnote_markers': parsed_value['footnote_markers'],
                    'scope_notes': [{'marker': note['marker'], 'text': note['text'],
                                     'bbox': note['bbox'], 'page': page_no} for note in scope_notes],
                    'note': parsed_value['note'],
                    'evidence': [{
                        'carrier': 'pdf', 'document': parsed['document'], 'page': page_no,
                        'rect': cell['bbox'] if cell else _cell_rect(page, leaf_col, band_index),
                        'line_rects': cell['bboxes'] if cell else [],
                        'table_id': table_id, 'row_label': [code, name],
                    }],
                    'pending_reasons': status_reasons,
                }
                observations.append(observation)
    return {'observations': observations, 'excluded': excluded, 'reviews': reviews,
            'tables': {k: {'table_id': v['table_id'], 'indicator': v['indicator'],
                           'unit': v['unit'], 'pages': v['pages'],
                           'columns': {str(k2): v2 for k2, v2 in v['columns'].items()},
                           'continuation_ok': v['continuation_ok']}
                       for k, v in tables.items()}}


def _row_text(cells):
    return ' | '.join(_cell_text(cells[key]) for key in sorted(cells))


def _row_bbox(cells):
    boxes = [cell['bbox'] for cell in cells.values() if 'bbox' in cell]
    if not boxes:
        return [0, 0, 0, 0]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _cell_rect(page, leaf_col, band_index):
    leaf = page['leafs'][leaf_col]
    band = page['bands'][band_index]
    return [leaf['x0'], band[0], leaf['x1'], band[1]]


def _header_bands(page):
    data_bands = _data_bands(page)
    return [i for i in sorted(page['rows']) if data_bands and i < min(data_bands)]


def _label_columns(page):
    """从表头行找出"城市代码"和"城市"所在的叶子列，而不是假定第 0/1 列。"""
    code_col, name_col = 0, 1
    for band_index in sorted(page['rows']):
        for (start, end), cell in page['rows'][band_index].items():
            text = _cell_text(cell)
            if text in ('城市代码', '地区代码', '代码'):
                code_col = start
            elif text in ('城市', '城市名称', '地区', '名称'):
                name_col = start
    return code_col, name_col


def _data_bands(page, code_col: int = 0):
    data = []
    for band_index in sorted(page['rows']):
        cells = page['rows'][band_index]
        code_cell = cells.get((code_col, code_col))
        code = _cell_text(code_cell) if code_cell else ''
        if CODE_RE.match(code):
            data.append(band_index)
    return data


def _column_model(page):
    """利用每行带实际的列边界展开合并表头，得到叶子列 → (指标, 年份)。"""
    header_bands = _header_bands(page)
    leafs = page['leafs']
    model = {}
    for leaf in leafs:
        col = leaf['index']
        pieces = []
        for band_index in header_bands:
            for (start, end), cell in page['rows'].get(band_index, {}).items():
                if start <= col <= end:
                    pieces.append(_cell_text(cell))
        pieces = [piece for piece in pieces if piece]
        if not pieces:
            continue
        year = None
        indicator = None
        for piece in pieces:
            from .normalize import parse_year
            candidate = parse_year(piece)
            if candidate is not None:
                if year is None:
                    year = candidate
                continue
            if indicator is None:
                indicator = piece
        # 城市代码/城市列没有年份和指标，不当作数据列。
        if year is None:
            continue
        model[col] = {'year': year, 'indicator': indicator, 'header': ' / '.join(pieces)}
    return model
