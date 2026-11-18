"""原文规范化。

这里的原则是"先保留原文，再解释原文"：

* 原始值文本（包括脚注上标、缺报符号、千分位）不会被覆盖；
* 只有明确的解释结果放进 value_text / numeric_value / value_status；
* 无法解释时返回 ``unparsed``，由上层放入人工核实队列，而不是猜一个数字。
"""
from __future__ import annotations

import re
import unicodedata

CIRCLED_MARKERS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
CIRCLED_SET = set(CIRCLED_MARKERS)
ELLIPSIS_CHARS = {'…', '⋯', '...', '．．．'}
DASH_CHARS = {'—', '–', '－', '−', '-', '——'}
NOT_APPLICABLE_WORDS = {'不适用', '无', '不涉及'}
MISSING_WORDS = {'缺报', '缺失', '未报', '未统计'}
NOT_PUBLISHED_WORDS = {'未刊载', '空白', '未公布'}
ZERO_WORDS = {'零', '零值'}

TOTAL_WORDS = ('合计', '总计', '小计', '全省', '全省合计', '省合计', '地区合计', '全国')
CITY_CODE_RE = re.compile(r'^C\d{2,}$', re.IGNORECASE)
PROVINCE_CODE_RE = re.compile(r'^P\d{2,}$', re.IGNORECASE)
CODE_RE = re.compile(r'^[A-Za-z]{1,3}\d{2,}$')
YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
TABLE_ID_RE = re.compile(r'表\s*([一二三四五六七八九十百零〇0-9]+)')
CONTINUATION_RE = re.compile(r'[（(]\s*续\s*[）)]')
FOOTNOTE_DEF_RE = re.compile(r'^\s*([①-⑳])\s*(.*)$')
BRACKET_MARKER_RE = re.compile(r'[\s]*[\[\(（]\s*(\d{1,2})\s*[\]\)）]\s*$')
UNIT_RE = re.compile(r'(?:更正单位|单位)\s*[:：]\s*([^；;，,。\n]+)')
EDITION_RE = re.compile(r'(?:^|[;；])\s*(first|second|third|第[一二三四五六七八九十0-9]+版)\s*$', re.IGNORECASE)
ERRATA_ID_RE = re.compile(r'勘误\s*([A-Za-z]\s*-\s*\d{4}\s*-\s*\d+)')


def clean_text(value) -> str:
    """只做无损清理：去零宽字符、统一空白，不删除符号和脚注。"""
    if value is None:
        return ''
    text = str(value)
    text = text.replace('​', '').replace('﻿', '').replace('\xa0', ' ')
    return re.sub(r'[ \t\r\n]+', ' ', text).strip()


def strip_footnote_markers(raw: str) -> tuple[str, list[str]]:
    """剥离脚注上标，保留剩余原文。支持圈码与行尾 [1]/(1) 形式。"""
    text = clean_text(raw)
    markers: list[str] = []

    def take(match: re.Match[str]) -> str:
        marker = match.group(0).strip()
        if marker and marker not in markers:
            markers.append(marker)
        return ''

    text = re.sub('[' + CIRCLED_MARKERS + ']', take, text)
    match = BRACKET_MARKER_RE.search(text)
    if match:
        marker = '[' + match.group(1) + ']'
        markers.append(marker)
        text = text[:match.start()].rstrip()
    return text.strip(), markers


def parse_numeric(text: str):
    """只接受单个纯数字。范围、约数、带单位混排都返回 None。"""
    candidate = unicodedata.normalize('NFKC', text)
    candidate = candidate.replace(',', '').replace('，', '').replace(' ', '')
    candidate = candidate.replace('＋', '+').replace('－', '-')
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', candidate):
        return None
    if re.fullmatch(r'[+-]?\d+', candidate):
        return int(candidate)
    return float(candidate)


def parse_value(raw, legend: dict | None = None) -> dict:
    """解释一个单元格原始值。

    返回字段：raw_value, value_text, numeric_value, value_status, footnote_markers,
    note（解释），unparsed（是否需要人工核实）。

    legend 是原件自己的符号说明（例如"⋯ 缺报"）。说明优先于内置含义；
    两者不一致时保留说明原文并标记待核实，而不是悄悄采用内置含义。
    """
    raw_text = clean_text(raw)
    value_text, markers = strip_footnote_markers(raw_text)
    normalized = value_text.strip()
    legend = {clean_text(k): clean_text(v) for k, v in (legend or {}).items()}
    legend_note = None
    unparsed = False

    if not normalized and raw_text:
        status = 'unparsed'
        numeric = None
        note = '单元格仅含脚注标记，数值位置无法判断'
        unparsed = True
    elif not normalized:
        status = 'not_published'
        numeric = None
        note = ('空白：' + legend['']) if '' in legend else '空白：原件表示未刊载'
        legend_note = legend.get('')
        unparsed = False
    elif normalized in legend:
        meaning = legend[normalized]
        if any(word in meaning for word in ('零',)):
            status, numeric = 'zero', 0
        elif any(word in meaning for word in ('不适用', '无')):
            status, numeric = 'not_applicable', None
        elif any(word in meaning for word in ('缺报', '缺失', '未报', '未统计')):
            status, numeric = 'missing', None
        elif any(word in meaning for word in ('未刊载', '空白', '未公布')):
            status, numeric = 'not_published', None
        else:
            status, numeric = 'unparsed', None
            unparsed = True
            note = '原件符号说明无法归类，保留原文待人工核实'
            legend_note = meaning
            return {
                'raw_value': raw_text,
                'value_text': normalized,
                'numeric_value': numeric,
                'value_status': status,
                'footnote_markers': markers,
                'note': note,
                'legend_note': legend_note,
                'unparsed': unparsed,
            }
        legend_note = meaning
        note = '按原件符号说明：' + meaning
        unparsed = False
    elif normalized in ZERO_WORDS or normalized == '0':
        status = 'zero'
        numeric = 0
        note = '符号 0：零值'
        unparsed = False
    elif normalized in NOT_APPLICABLE_WORDS or normalized in DASH_CHARS:
        status = 'not_applicable'
        numeric = None
        note = '破折号：不适用'
        unparsed = False
    elif normalized in MISSING_WORDS or normalized in ELLIPSIS_CHARS:
        status = 'missing'
        numeric = None
        note = '省略号/缺报标记：原件未给出数值'
        unparsed = False
    elif normalized in NOT_PUBLISHED_WORDS:
        status = 'not_published'
        numeric = None
        note = '未刊载'
        unparsed = False
    else:
        numeric = parse_numeric(normalized)
        if numeric is None:
            status = 'unparsed'
            note = '无法作为单一数值解释，保留原文待人工核实'
            unparsed = True
        else:
            status = 'value'
            note = '数值'
            unparsed = False

    return {
        'raw_value': raw_text,
        'value_text': normalized,
        'numeric_value': numeric,
        'value_status': status,
        'footnote_markers': markers,
        'note': note,
        'legend_note': legend_note,
        'unparsed': unparsed,
    }


def parse_unit(text: str) -> str | None:
    match = UNIT_RE.search(clean_text(text))
    if not match:
        return None
    unit = clean_text(match.group(1))
    return unit or None


def parse_year(text: str) -> int | None:
    match = YEAR_RE.search(clean_text(text))
    if not match:
        return None
    return int(match.group(1))


def parse_table_id(text: str) -> str | None:
    match = TABLE_ID_RE.search(clean_text(text))
    if not match:
        return None
    return '表' + match.group(1)


def is_continuation(text: str) -> bool:
    return bool(CONTINUATION_RE.search(clean_text(text)))


def parse_declared(text: str) -> dict:
    """解析"年鉴 2025；first"这类声明，不把它当成观测年。"""
    text = clean_text(text)
    publication_year = None
    match = re.search(r'(?:城市能源年鉴|能源年鉴|年鉴)\s*((?:19|20)\d{2})', text)
    if match:
        publication_year = int(match.group(1))
    else:
        year = parse_year(text)
        # 仅当文本确实在声明年鉴/出版时才猜测，避免把表内观测年当出版年。
        if year is not None and ('出版' in text or '年鉴' in text):
            publication_year = year

    edition = None
    match = EDITION_RE.search(text)
    if match:
        edition = match.group(1).lower()

    errata = None
    match = ERRATA_ID_RE.search(text)
    if match:
        errata = re.sub(r'\s+', '', match.group(1)).upper()

    return {
        'publication_year': publication_year,
        'edition': edition,
        'errata_id': errata,
        'declared_text': text,
    }


def merge_declared(items: list[dict]) -> dict:
    """合并一份文件内多页的声明；只接受不矛盾的信息。"""
    merged = {'publication_year': None, 'edition': None, 'errata_id': None,
              'declared_text': '', 'sources': []}
    conflicts = []
    for item in items:
        if not item:
            continue
        for key in ('publication_year', 'edition', 'errata_id'):
            value = item.get(key)
            if value is None:
                continue
            if merged[key] is None:
                merged[key] = value
            elif merged[key] != value:
                conflicts.append({'field': key, 'values': [merged[key], value],
                                  'texts': [merged['declared_text'], item.get('declared_text')]})
        if item.get('declared_text'):
            merged['sources'].append(item['declared_text'])
            if not merged['declared_text']:
                merged['declared_text'] = item['declared_text']
    merged['conflicts'] = conflicts
    return merged


def classify_row(code: str, name: str) -> dict:
    """返回行角色。未知行绝不默认当成城市。"""
    code = clean_text(code)
    name = clean_text(name)
    name_no_space = re.sub(r'\s+', '', name)
    is_total_word = any(word in name_no_space for word in TOTAL_WORDS)
    is_province_name = '省' in name_no_space or '全省' in name_no_space

    if PROVINCE_CODE_RE.match(code) or (is_total_word and is_province_name):
        return {'role': 'province_total', 'reason': '省合计/省级合计标记，不进入城市记录'}
    if is_total_word:
        return {'role': 'subtotal', 'reason': '合计/小计标记，不进入城市记录'}
    if CITY_CODE_RE.match(code):
        return {'role': 'city', 'reason': '城市代码 C 开头'}
    if not code and not name:
        return {'role': 'blank', 'reason': '空行'}
    return {'role': 'unclassified', 'reason': '行角色无法由代码和名称判断'}


def scope_mentions(note_text: str, city_code: str, city_name: str, year: int | None) -> bool:
    """判断一条表注是否在说明某个城市/年份的统计范围。"""
    text = clean_text(note_text)
    if not text:
        return False
    mentions_city = (city_code and city_code in text) or (city_name and city_name in text)
    mentions_year = year is None or str(year) in text
    return bool(mentions_city and mentions_year)
