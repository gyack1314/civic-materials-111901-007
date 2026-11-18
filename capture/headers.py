"""PDF 表头与页脚符号说明解析。"""
from __future__ import annotations

import re

from .normalize import (clean_text, is_continuation, parse_table_id, parse_unit,
                        parse_year)

LEGEND_INTRO = ('符号', '注', '说明')
LEGEND_RE = re.compile(r'([0-9０-９]+|[—–－⋯…]+|空白)(?:\s*)([^；;。]{1,20})')


def extract_headings(blocks: list[dict]) -> list[dict]:
    """从 PDF 文本块中取出标题行（表名、声明行）与疑似脚注/符号说明块。

    blocks 由 pdf.py 提供：{'text','bbox','page','size','y'}。
    返回 {'title_lines','continuation','declared_lines','legend_blocks','footnote_blocks'}。
    """
    title_lines, declared_lines = [], []
    legend_blocks, footnote_blocks = [], []
    continuation = False
    for block in blocks:
        text = clean_text(block.get('text'))
        if not text:
            continue
        if text.startswith('表'):
            title_lines.append({'text': text, 'bbox': block['bbox'], 'page': block['page'],
                                'continuation': is_continuation(text)})
            continuation = continuation or is_continuation(text)
        elif (not title_lines) and ('年鉴' in text or '出版' in text or '勘误' in text):
            declared_lines.append({'text': text, 'bbox': block['bbox'], 'page': block['page']})
        elif text.startswith(LEGEND_INTRO) or any(word in text for word in ('零值', '缺报', '未刊载', '不适用')):
            legend_blocks.append({'text': text, 'bbox': block['bbox'], 'page': block['page']})
        elif text and text[0] in '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳':
            footnote_blocks.append({'text': text, 'bbox': block['bbox'], 'page': block['page']})
    return {
        'title_lines': title_lines,
        'continuation': continuation,
        'declared_lines': declared_lines,
        'legend_blocks': legend_blocks,
        'footnote_blocks': footnote_blocks,
    }


def parse_legend(text: str) -> dict:
    """把"符号：0 零值；空白 未刊载；— 不适用；⋯ 缺报。"解析为符号→含义。

    只做拆分，不判断真伪；返回原文以便在证据中回查。
    """
    text = clean_text(text)
    text = re.sub(r'^(符号|注|说明)\s*[:：]\s*', '', text)
    text = text.rstrip('。.')
    parts = re.split(r'[；;]', text)
    legend = {}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        match = LEGEND_RE.match(part)
        if not match:
            continue
        symbol, meaning = match.group(1), match.group(2).strip()
        if symbol == '空白':
            legend[''] = meaning or '空白'
            continue
        legend[symbol] = meaning
    return {'text': text, 'map': legend}


def merge_titles(titles: list[dict]) -> dict | None:
    """合并同一页的多行表名（续表常见"表一（续）…"与"表一 指标；单位…"）。

    表号必须一致；指标和单位取明确声明的那一行；只要有一行声明续页，
    整页就按续页处理。
    """
    if not titles:
        return None
    # 续页同时出现"表一（续）…"与"表一 指标；单位…"时，以带指标/单位的那行
    # 作为表号来源；若两行表号不同，保留冲突。
    parsed = []
    for title in titles:
        info = parse_title(title['text'])
        info.update({key: value for key, value in title.items()
                     if key in ('bbox', 'page', 'continuation')})
        parsed.append(info)
    ids = {item['table_id'] for item in parsed if item['table_id']}
    if len(ids) > 1:
        return {'table_id': None, 'indicator': None, 'unit': None,
                'continuation': False, 'text': ' / '.join(t['text'] for t in titles),
                'conflict': sorted(ids)}
    merged = {
        'table_id': next(iter(ids), None),
        'indicator': next((t['indicator'] for t in parsed if t.get('indicator')), None),
        'unit': next((t['unit'] for t in parsed if t.get('unit')), None),
        'continuation': any(t.get('continuation') for t in parsed),
        'text': ' / '.join(t['text'] for t in titles),
        'bboxes': [t.get('bbox') for t in parsed if t.get('bbox')],
        'sources': [{'text': t['text'], 'bbox': t.get('bbox')} for t in parsed],
    }
    return merged


def parse_title(text: str) -> dict:
    """解析表名行："表一 终端能源；单位：万吨标准煤"。"""
    text = clean_text(text)
    body = re.sub(r'^\s*表\s*[一二三四五六七八九十百零〇0-9]+\s*(?:[（(]续[）)])?\s*', '', text)
    head = re.split(r'[；;]', body)[0]
    indicator = None
    if parse_unit(text) is not None and head.strip():
        indicator = head.strip()
    elif '年鉴' in text or '出版' in text:
        # "表一（续）城市能源年鉴 2025"只是书名行，不是指标名。
        indicator = None
    return {
        'table_id': parse_table_id(text),
        'indicator': indicator,
        'unit': parse_unit(text),
        'continuation': is_continuation(text),
        'text': text,
    }


def year_columns(header_cells: list[dict]) -> dict[int, int]:
    """从表头词中找出观测年列。返回 {列序号: 年份}。

    列序号由 pdf.py 根据竖线位置给出；这里只负责识别形如 2023/2024 的词，
    并拒绝同一列出现两个不同年份。
    """
    columns: dict[int, int] = {}
    conflicts = []
    for cell in header_cells:
        year = parse_year(cell['text'])
        if year is None:
            continue
        col = cell['col']
        if col in columns and columns[col] != year:
            conflicts.append({'col': col, 'years': [columns[col], year]})
        columns.setdefault(col, year)
    return {'columns': columns, 'conflicts': conflicts}
