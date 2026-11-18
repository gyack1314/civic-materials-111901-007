"""采集记录模型：证据标识、观测值合并、待核实与人工确认。"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

STATUS_MEANINGS = {
    'value': '原件刊载数值',
    'zero': '原值为 0，表示零值；不是缺报',
    'not_published': '原件为空白，表示未刊载',
    'not_applicable': '原件用破折号表示不适用',
    'missing': '原件用缺报符号表示未统计/缺报',
    'unparsed': '原件文本无法解释为单一数值，保留原文',
}


def evidence_id(evidence: dict) -> str:
    """证据位置的可读标识：PDF 页码+矩形，或工作簿表名+单元格。"""
    if evidence.get('carrier') == 'pdf':
        rect = evidence.get('rect') or [0, 0, 0, 0]
        rounded = [round(float(value), 2) for value in rect]
        return (f"pdf:{evidence.get('document')}@p{evidence.get('page')}"
                f"[{','.join(map(str, rounded))}]")
    if evidence.get('carrier') == 'workbook':
        return (f"workbook:{evidence.get('document')}#{evidence.get('sheet')}"
                f"!{evidence.get('cell')}")
    return 'unknown:' + hashlib.sha1(
        repr(sorted(evidence.items())).encode()).hexdigest()[:12]


def citation(evidence: dict) -> str:
    if evidence.get('carrier') == 'pdf':
        rect = evidence.get('rect') or [0, 0, 0, 0]
        return (f"{evidence.get('document')} 第 {evidence.get('page')} 页，"
                f"矩形 [{', '.join(f'{float(v):.1f}' for v in rect)}]（PDF 点，左上角原点）")
    if evidence.get('carrier') == 'workbook':
        return (f"{evidence.get('document')} 工作表 {evidence.get('sheet')}，"
                f"单元格 {evidence.get('cell')}")
    return '位置未知'


def observation_key(observation: dict) -> str:
    """城市 + 观测期 + 指标 是一条城市数据的身份，不含载体和版本。"""
    return '|'.join([
        str(observation.get('city_code') or ''),
        str(observation.get('observation_year') or ''),
        str(observation.get('indicator') or ''),
    ])


def load_parsed(manifest: dict) -> dict:
    """解析 manifest 中的全部原件，返回按记录组织的载体结果与待核实项。"""
    from .pdf_parser import build_page_tables, parse_pdf
    from .workbook_parser import parse_workbook

    results = {'records': {}, 'reviews': []}
    for record in manifest['records']:
        record_id = record['id']
        entry = {'id': record_id, 'corrects': record.get('corrects'),
                 'declared': {}, 'carriers': {}}
        carriers = record.get('carriers', {})
        if record.get('corrects'):
            # 勘误原件由 errata 模块按其自身版式解释；这里只保留声明，
            # 避免把"替换范围/更正单位"等说明行误报为表格几何问题。
            entry['declared'] = _errata_declarations(manifest, carriers)
            results['records'][record_id] = entry
            continue
        if 'pdf' in carriers:
            path = _resolve(manifest, carriers['pdf'])
            pdf = parse_pdf(path.read_bytes(), path.name)
            tables = build_page_tables(pdf)
            entry['declared']['pdf'] = pdf['declared']
            entry['carriers']['pdf'] = {
                'document': path.name,
                'path': carriers['pdf']['path'],
                'sha256': carriers['pdf']['sha256'],
                'etag': carriers['pdf'].get('etag'),
                'declared': pdf['declared'],
                'legend': pdf['legend'],
                'legend_texts': pdf['legend_texts'],
                'footnotes': pdf['footnotes'],
                'tables': tables['tables'],
                'observations': tables['observations'],
                'excluded': tables['excluded'],
            }
            for review in pdf['reviews'] + tables['reviews']:
                results['reviews'].append(dict(review, carrier='pdf', record=record_id,
                                               document=path.name))
        if 'workbook' in carriers:
            path = _resolve(manifest, carriers['workbook'])
            workbook = parse_workbook(path.read_bytes(), path.name)
            entry['declared']['workbook'] = workbook['declared']
            observations, excluded = [], []
            for sheet in workbook['sheets']:
                observations.extend(sheet['observations'])
                excluded.extend(sheet['excluded'])
            entry['carriers']['workbook'] = {
                'document': path.name,
                'path': carriers['workbook']['path'],
                'sha256': carriers['workbook']['sha256'],
                'etag': carriers['workbook'].get('etag'),
                'declared': workbook['declared'],
                'legend': workbook['legend'],
                'sheets': workbook['sheets'],
                'observations': observations,
                'excluded': excluded,
            }
            for review in workbook['reviews']:
                results['reviews'].append(dict(review, carrier='workbook',
                                               record=record_id, document=path.name))
        results['records'][record_id] = entry
    return results


def _errata_declarations(manifest: dict, carriers: dict) -> dict:
    from .errata import parse_errata_pdf, parse_errata_workbook

    declared = {}
    for carrier in ('pdf', 'workbook'):
        if carrier not in carriers:
            continue
        path = _resolve(manifest, carriers[carrier])
        data = path.read_bytes()
        parsed = (parse_errata_pdf(data, path.name) if carrier == 'pdf'
                  else parse_errata_workbook(data, path.name))
        declared[carrier] = parsed['declared']
    return declared


def _resolve(manifest: dict, carrier: dict) -> Path:
    path = Path(carrier['path'])
    if not path.is_absolute():
        path = Path(manifest['_destination']) / path
    return path


def reading(observation: dict) -> dict:
    evidence = observation.get('evidence') or []
    return {
        'carrier': observation.get('carrier'),
        'document': observation.get('document'),
        'record_id': observation.get('record_id'),
        'city_code': observation.get('city_code'),
        'city_name': observation.get('city_name'),
        'observation_year': observation.get('observation_year'),
        'indicator': observation.get('indicator'),
        'table_id': observation.get('table_id'),
        'column_header': observation.get('column_header'),
        'raw_value': observation.get('raw_value'),
        'value_text': observation.get('value_text'),
        'numeric_value': observation.get('numeric_value'),
        'value_status': observation.get('value_status'),
        'value_status_meaning': STATUS_MEANINGS.get(observation.get('value_status') or '',
                                                    '未知状态'),
        'unit': observation.get('unit'),
        'footnote_markers': observation.get('footnote_markers') or [],
        'scope_notes': observation.get('scope_notes') or [],
        'note': observation.get('note'),
        'evidence': evidence,
        'evidence_ids': [evidence_id(item) for item in evidence],
        'citations': [citation(item) for item in evidence],
        'pending_reasons': observation.get('pending_reasons') or [],
    }


def _review_id(kind: str, detail) -> str:
    digest = hashlib.sha256((kind + repr(detail)).encode('utf-8')).hexdigest()[:10]
    return f'R-{kind}-{digest}'


def make_review(kind: str, message: str, evidence=None, detail=None,
                record_id=None, blocks_key=None) -> dict:
    review = {
        'id': _review_id(kind, (message, detail, evidence)),
        'kind': kind,
        'message': message,
        'detail': detail,
        'evidence': evidence or [],
        'evidence_ids': [evidence_id(item) for item in (evidence or [])],
        'record_id': record_id,
        'observation_key': blocks_key,
    }
    return review


def _same_reading(left: dict, right: dict) -> bool:
    return (left.get('numeric_value') == right.get('numeric_value')
            and left.get('value_status') == right.get('value_status')
            and left.get('unit') == right.get('unit'))


def assemble(parsed: dict, errata: list[dict], decisions: dict | None = None) -> dict:
    """合并各载体读数、勘误和人工决定，生成可交付记录与待核实队列。

    原则：
    * 同一城市/年/指标在不同载体不一致时，不以任何载体为默认真值；
    * 勘误只改它明确指向的单元，并带上它自己的单位；
    * 一切判定都保留证据位置与原因。
    """
    decisions = copy.deepcopy((decisions or {}).get('decisions', {}))
    base_records = [record for record in parsed['records'].values()
                    if not record.get('corrects')]
    readings_by_key: dict[str, list[dict]] = {}
    excluded, reviews = [], list(parsed.get('reviews', []))
    carrier_payloads = []
    for record in base_records:
        for carrier, payload in record['carriers'].items():
            carrier_payloads.append((record, carrier, payload))
            for observation in payload['observations']:
                item = reading(dict(observation, record_id=record['id']))
                readings_by_key.setdefault(observation_key(observation), []).append(item)
            for row in payload.get('excluded', []):
                excluded.append(dict(row, record_id=record['id'], carrier=carrier))
            for pending in payload.get('pending_reasons', []) or []:
                pass

    # 每个城市的读数也要能看见"因行角色被排除"的省合计。
    records = []
    for key, readings in sorted(readings_by_key.items()):
        record = _assemble_record(key, readings, decisions)
        records.append(record)
    records.sort(key=lambda item: (item['city_code'] or '', item['observation_year'] or 0,
                                   item['indicator'] or ''))

    # 勘误应用：目标必须精确到表/城市/观测年。
    errata_applied, errata_reviews, resolved_keys = _apply_errata(
        records, errata, decisions)
    reviews.extend(errata_reviews)

    for record in records:
        if record['key'] in resolved_keys:
            record['carrier_conflict'] = False
            record['pending_reasons'] = [
                reason for reason in record['pending_reasons']
                if reason['reason'] != 'carrier_conflict']
            record['resolved_by'] = resolved_keys[record['key']]
        if record.get('carrier_conflict'):
            review = make_review(
                'carrier_conflict',
                f"{record['city_code']} {record['observation_year']} "
                f"{record['indicator']} 的两种载体读数不一致，需人工确认采用哪一条",
                evidence=record.get('conflict_evidence'),
                detail={'key': record['key'], 'readings': record['readings']},
                blocks_key=record['key'])
            if record['status'] == 'confirmed' and record.get('decision'):
                # 人工已经选定了载体；核实项留档但标记为已处理。
                review['resolved_by'] = '人工确认：' + (
                    record['decision'].get('note') or record['decision'].get('evidence_id') or '')
            else:
                record['pending_review_ids'].append(review['id'])
            reviews.append(review)
            for reason in record.get('pending_reasons', []):
                if reason['reason'] == 'carrier_conflict':
                    reason['review_id'] = review['id']
        for reason in record.get('pending_reasons', []):
            if reason.get('review_id'):
                continue
            review = make_review(
                'observation_pending', reason['message'],
                evidence=reason.get('evidence'),
                detail={'key': record['key'], 'reason': reason['reason']},
                record_id=reason.get('record_id'),
                blocks_key=record['key'])
            reason['review_id'] = review['id']
            record['pending_review_ids'].append(review['id'])
            reviews.append(review)

    for review in reviews:
        if review.get('observation_key') in resolved_keys and \
                review.get('kind') in ('carrier_conflict', 'observation_pending'):
            review['resolved_by'] = resolved_keys[review['observation_key']]
    issues = _dedupe_reviews(reviews)
    return {
        'records': records,
        'excluded': excluded,
        'reviews': issues,
        'errata': errata_applied,
        'carrier_checks': _crosscheck_carriers(carrier_payloads),
    }


def _assemble_record(key: str, readings: list[dict], decisions: dict) -> dict:
    parts = key.split('|')
    city_code, year_text, indicator = parts[0], parts[1], parts[2]
    observation_year = int(year_text) if year_text.isdigit() else None
    city_name = next((item.get('city_name') for item in readings if item.get('city_name')),
                     None)
    table_ids = sorted({item.get('table_id') for item in readings if item.get('table_id')})
    units = sorted({item.get('unit') for item in readings if item.get('unit')})
    scope_notes = _merge_notes(items['scope_notes'] for items in readings)

    record = {
        'key': key,
        'city_code': city_code,
        'city_name': city_name,
        'observation_year': observation_year,
        'indicator': indicator,
        'table_ids': table_ids,
        'units': units,
        'readings': readings,
        'chosen': None,
        'status': 'pending',
        'pending_reasons': [],
        'pending_review_ids': [],
        'superseded': [],
        'scope_notes': scope_notes,
        'carrier_conflict': False,
        'conflict_evidence': [],
        'corroborated': False,
        'decision': decisions.get(key),
    }

    value_readings = [item for item in readings if item.get('value_status') == 'value']
    zero_readings = [item for item in readings if item.get('value_status') == 'zero']
    unavailable = [item for item in readings
                   if item.get('value_status') in
                   ('missing', 'not_published', 'not_applicable', 'unparsed')]

    # 载体冲突：同一身份下的有效读数不一致，或"有数"与"无数"并存。
    if len(readings) > 1:
        reference = readings[0]
        conflicts = [item for item in readings[1:] if not _same_reading(reference, item)]
        if conflicts:
            record['carrier_conflict'] = True
            record['conflict_evidence'] = [
                evidence for item in readings for evidence in item.get('evidence', [])]
            record['pending_reasons'].append({
                'reason': 'carrier_conflict',
                'message': '两种载体刊值不一致，保留各自原始证据待人工确认',
            })
    if table_ids and len(table_ids) > 1:
        record['pending_reasons'].append({
            'reason': 'table_mismatch',
            'message': '同一城市/观测期/指标出现在不同表号，统计范围可能不同',
        })
    names = sorted({item.get('city_name') for item in readings if item.get('city_name')})
    if len(names) > 1:
        record['city_names'] = names
        record['pending_reasons'].append({
            'reason': 'city_name_variance',
            'message': '同一城市代码在不同表/载体中名称不同（' + '、'.join(names)
                       + '），可能是更名或不同统计口径',
        })

    usable = value_readings or zero_readings
    if not usable:
        record['status'] = 'unavailable'
        if unavailable:
            first = unavailable[0]
            record['value_status'] = first.get('value_status')
            record['status_meaning'] = first.get('value_status_meaning')
            record['status_note'] = first.get('note')
        else:
            record['value_status'] = 'unparsed'
            record['status_meaning'] = STATUS_MEANINGS['unparsed']
        record['pending_reasons'].extend(_unit_reasons(readings))
        record['pending_reasons'].extend(_unit_mismatch_reasons(readings))
    elif record['carrier_conflict']:
        record['status'] = 'conflict'
        record['value_status'] = None
        record['status_meaning'] = '载体读数冲突，暂不采用'
    else:
        chosen = usable[0]
        agreed = all(_same_reading(chosen, item) for item in usable)
        record['chosen'] = _chosen_from(chosen, '原件读数')
        record['value_status'] = chosen.get('value_status')
        record['status_meaning'] = chosen.get('value_status_meaning')
        record['corroborated'] = agreed and len(usable) > 1
        record['status'] = 'adopted'
        record['pending_reasons'].extend(_unit_reasons(readings))
        record['pending_reasons'].extend(_unit_mismatch_reasons(readings))
        record['pending_reasons'].extend(_unavailable_reasons(readings))

    decision = decisions.get(key)
    if decision and decision.get('action') == 'choose':
        target = next((item for item in readings
                       if decision.get('evidence_id') in item.get('evidence_ids', [])), None)
        if target:
            record['chosen'] = _chosen_from(target, '人工确认：' + (decision.get('note') or ''))
            record['status'] = 'confirmed'
            record['value_status'] = target.get('value_status')
            record['status_meaning'] = target.get('value_status_meaning')
            record['decision'] = decision
            # 人工确认只处理"选哪个载体/值"；缺单位、范围脚注等仍保留在记录上。
            resolved = {'carrier_conflict', 'unit_mismatch', 'carrier_value_unavailable',
                        'decision_not_found'}
            record['pending_reasons'] = [reason for reason in record['pending_reasons']
                                         if reason['reason'] not in resolved
                                         and not reason.get('review_id')]
        else:
            record['pending_reasons'].append({
                'reason': 'decision_not_found',
                'message': '人工确认指向的证据不在本记录中，决定未生效',
            })
    elif decision and decision.get('action') == 'keep_pending':
        record['status'] = 'pending'
        record['decision'] = decision

    return record


def _chosen_from(item: dict, basis: str) -> dict:
    return {
        'carrier': item.get('carrier'),
        'document': item.get('document'),
        'raw_value': item.get('raw_value'),
        'numeric_value': item.get('numeric_value'),
        'unit': item.get('unit'),
        'value_status': item.get('value_status'),
        'evidence': item.get('evidence') or [],
        'evidence_ids': item.get('evidence_ids'),
        'citations': item.get('citations'),
        'basis': basis,
    }


def _unit_reasons(readings: list[dict]) -> list[dict]:
    reasons = []
    for item in readings:
        if not item.get('unit'):
            reasons.append({
                'reason': 'missing_unit',
                'message': f"{item.get('carrier')} 载体未声明单位，不能沿用其他载体单位",
                'evidence': item.get('evidence'),
                'record_id': item.get('record_id'),
            })
    return reasons


def _unit_mismatch_reasons(readings: list[dict]) -> list[dict]:
    """两个载体数值一致但单位不同：同样不能默认采用任一单位。"""
    units = {item.get('unit') for item in readings if item.get('unit')}
    if len(units) < 2:
        return []
    return [{
        'reason': 'unit_mismatch',
        'message': '两种载体单位不同（' + '、'.join(sorted(units)) + '），需人工确认换算依据',
        'evidence': [evidence for item in readings for evidence in item.get('evidence', [])],
    }]


def _unavailable_reasons(readings: list[dict]) -> list[dict]:
    reasons = []
    for item in readings:
        if item.get('value_status') in ('missing', 'not_applicable', 'not_published', 'unparsed'):
            reasons.append({
                'reason': 'carrier_value_unavailable',
                'message': ('同一记录的某载体为'
                            + item.get('value_status_meaning', '不可用')
                            + '，已与有效读数并列保留'),
                'evidence': item.get('evidence'),
                'record_id': item.get('record_id'),
            })
    return reasons


def _merge_notes(note_lists) -> list[dict]:
    seen, merged = set(), []
    for notes in note_lists:
        for note in notes or []:
            key = (note.get('marker'), note.get('text'))
            if key in seen:
                continue
            seen.add(key)
            merged.append(note)
    return merged


def _apply_errata(records: list[dict], errata: list[dict],
                  decisions: dict) -> tuple[list, list, dict]:
    reviews = []
    applied = []
    resolved: dict[str, str] = {}
    by_key = {record['key']: record for record in records}
    for document in errata:
        target_table = document.get('table_id')
        targets = []
        conflicts_by_key = {
            (item['key'][0], item['key'][1]): item
            for item in document.get('conflicts', [])
            if item.get('kind') == 'errata_replacement_conflict'}
        handled_conflicts = set()
        for replacement in document['replacements']:
            unit_key = (replacement.get('city_code'), replacement.get('observation_year'))
            if unit_key in conflicts_by_key:
                if unit_key in handled_conflicts:
                    continue
                handled_conflicts.add(unit_key)
                conflict = conflicts_by_key[unit_key]
                review = make_review(
                    'errata_replacement_conflict',
                    f"勘误 {document['id']} 的同一替换单元在两个载体中不一致，"
                    f"未应用，保留双方原文",
                    evidence=[item.get('evidence') for item in conflict['readings']],
                    detail={'replacement': replacement, 'conflict': conflict},
                    record_id=document['id'])
                reviews.append(review)
                targets.append({'replacement': replacement, 'applied': False,
                                'review_id': review['id']})
                continue
            key_parts = [replacement.get('city_code') or '',
                         str(replacement.get('observation_year') or '')]
            candidates = [key for key in by_key
                          if key.split('|')[0] == key_parts[0]
                          and key.split('|')[1] == key_parts[1]]
            if target_table:
                candidates = [key for key in candidates
                              if target_table in (by_key[key].get('table_ids') or [target_table])]
            if len(candidates) != 1:
                detail = {'replacement': replacement, 'candidates': candidates,
                          'table_id': target_table}
                review = make_review(
                    'errata_target_unresolved',
                    f"勘误 {document['id']} 的替换目标无法唯一对应到城市记录",
                    evidence=[replacement.get('evidence')],
                    detail=detail,
                    record_id=document['id'])
                reviews.append(review)
                targets.append({'replacement': replacement, 'applied': False,
                                'review_id': review['id']})
                continue
            key = candidates[0]
            record = by_key[key]
            unit = replacement.get('unit') or document.get('unit')
            if not unit:
                review = make_review(
                    'errata_missing_unit',
                    f"勘误 {document['id']} 的替换单元没有声明单位，未采用，未沿用旧单位",
                    evidence=[replacement.get('evidence')],
                    detail={'key': key, 'replacement': replacement},
                    blocks_key=key)
                reviews.append(review)
                record['pending_reasons'].append({
                    'reason': 'errata_missing_unit',
                    'message': '勘误替换缺少单位，暂不采用',
                    'review_id': review['id'],
                })
                targets.append({'replacement': replacement, 'applied': False,
                                'review_id': review['id']})
                continue
            decision = decisions.get(key)
            overrides_decision = None
            if decision and decision.get('action') == 'choose' \
                    and decision.get('evidence_id') not in (replacement.get('evidence_ids') or []):
                # 后到勘误与旧的人工确认相反：勘误是对该单元的正式更正，予以采用，
                # 但把人工确认原样留档，并生成核实项提醒使用者复核。
                overrides_decision = {
                    'action': decision.get('action'),
                    'evidence_id': decision.get('evidence_id'),
                    'note': decision.get('note'),
                    'reviewer': decision.get('reviewer'),
                    'decided_at': decision.get('decided_at'),
                }
            observation_year = replacement.get('observation_year')
            superseded = [item for item in record['readings']
                          if item.get('observation_year') == observation_year
                          and item.get('carrier') in ('pdf', 'workbook')
                          and not item.get('superseded_by')]
            if not superseded and record.get('chosen', {}) and \
                    record['chosen'].get('errata_id') == document['id']:
                # 同一勘误的第二个载体：不重复替换，但要核对它是否与已应用读数一致。
                existing = record['chosen']
                same = (str(existing.get('raw_value')) == str(replacement.get('raw_value'))
                        and existing.get('unit') == (replacement.get('unit') or unit))
                if not same:
                    review = make_review(
                        'errata_carrier_mismatch',
                        f"勘误 {document['id']} 的两个载体对同一替换单元给出不同读数，"
                        f"已应用读数与会话中的另一载体不一致，请复核",
                        evidence=[replacement.get('evidence')],
                        detail={'key': key, 'applied': existing,
                                'other_carrier': replacement},
                        record_id=document['id'],
                        blocks_key=key)
                    reviews.append(review)
                targets.append({'replacement': replacement, 'applied': same, 'key': key,
                                'previous': existing.get('previous') or [],
                                'note': ('该替换单元已应用且两载体一致' if same else
                                         '该替换单元已应用，但另一载体读数不同，待复核')})
                continue
            record['superseded'].extend({
                'reading': item,
                'reason': f"被勘误 {document['id']} 替换",
                'errata_id': document['id'],
            } for item in superseded)
            numeric = replacement.get('numeric_value')
            if numeric is None:
                from .normalize import parse_numeric
                numeric = parse_numeric(replacement.get('raw_value') or '')
            if overrides_decision:
                review = make_review(
                    'errata_overrides_decision',
                    f"勘误 {document['id']} 替换了此前人工确认的读数，请复核",
                    evidence=[replacement.get('evidence')],
                    detail={'key': key, 'decision': overrides_decision,
                            'replacement': replacement},
                    record_id=document['id'],
                    blocks_key=key)
                reviews.append(review)
            chosen = {
                'carrier': replacement.get('carrier'),
                'document': (replacement.get('evidence') or {}).get('document'),
                'raw_value': replacement.get('raw_value'),
                'numeric_value': numeric,
                'unit': unit,
                'value_status': 'value' if numeric is not None else 'unparsed',
                'evidence': [replacement.get('evidence') or {}],
                'evidence_ids': [evidence_id(replacement.get('evidence') or {})],
                'citations': [citation(replacement.get('evidence') or {})],
                'basis': (f"勘误 {document['id']} 明确替换（原单位随替换单元更新）"
                          + ('；此前人工确认已被勘误取代，待复核' if overrides_decision else '')),
                'errata_id': document['id'],
                'overrides_decision': overrides_decision,
                'previous': [{
                    'raw_value': item.get('raw_value'),
                    'numeric_value': item.get('numeric_value'),
                    'unit': item.get('unit'),
                    'carrier': item.get('carrier'),
                    'document': item.get('document'),
                    'evidence': item.get('evidence') or [],
                    'evidence_ids': item.get('evidence_ids'),
                    'citations': item.get('citations'),
                } for item in superseded],
            }
            for item in superseded:
                item['superseded_by'] = document['id']
            record['chosen'] = chosen
            record['status'] = 'corrected'
            record['value_status'] = chosen['value_status']
            record['status_meaning'] = STATUS_MEANINGS.get(chosen['value_status'], '')
            record['units'] = sorted(set(record['units']) | {unit})
            record['pending_reasons'] = [reason for reason in record['pending_reasons']
                                         if reason['reason'] != 'carrier_conflict']
            resolved[key] = document['id']
            targets.append({'replacement': replacement, 'applied': True, 'key': key,
                            'previous': chosen['previous'],
                            'overrides_decision': overrides_decision})
        applied.append({
            'id': document['id'],
            'corrects': document['corrects'],
            'table_id': target_table,
            'scope': document.get('scope'),
            'unit': document.get('unit'),
            'carriers': [carrier.get('document') for carrier in document.get('carriers', [])],
            'targets': targets,
            'warnings': document.get('warnings', []),
        })
        for warning in document.get('warnings', []):
            reviews.append(make_review(
                warning['kind'], '勘误两载体声明不一致：' + warning['kind'],
                detail=warning, record_id=document['id']))
    return applied, reviews, resolved


def _crosscheck_carriers(carrier_payloads) -> list[dict]:
    """核对 PDF 与工作簿的声明版本、列覆盖与单位声明。"""
    by_record: dict[str, dict] = {}
    for record, carrier, payload in carrier_payloads:
        by_record.setdefault(record['id'], {})[carrier] = payload
    checks = []
    for record_id, carriers in by_record.items():
        if 'pdf' not in carriers or 'workbook' not in carriers:
            continue
        pdf, workbook = carriers['pdf'], carriers['workbook']
        checks.append({
            'record': record_id,
            'kind': 'declaration',
            'status': 'checked',
            'pdf': pdf.get('declared'),
            'workbook': workbook.get('declared'),
            'conflicts': _declaration_conflicts(pdf.get('declared') or {},
                                                workbook.get('declared') or {}),
        })
        pdf_columns = _pdf_columns(pdf)
        workbook_columns = _workbook_columns(workbook)
        checks.append({
            'record': record_id,
            'kind': 'columns',
            'status': 'checked',
            'pdf': pdf_columns,
            'workbook': workbook_columns,
            'diff': {
                'only_pdf': sorted(set(pdf_columns) - set(workbook_columns)),
                'only_workbook': sorted(set(workbook_columns) - set(pdf_columns)),
                'common': sorted(set(pdf_columns) & set(workbook_columns)),
            },
        })
        checks.append({
            'record': record_id,
            'kind': 'unit',
            'status': 'checked',
            'pdf': _pdf_units(pdf),
            'workbook': _workbook_units(workbook),
        })
    return checks


def _declaration_conflicts(left: dict, right: dict) -> list[dict]:
    conflicts = []
    for field in ('publication_year', 'edition', 'errata_id'):
        if left.get(field) is not None and right.get(field) is not None \
                and left.get(field) != right.get(field):
            conflicts.append({'field': field, 'pdf': left[field],
                              'workbook': right[field],
                              'texts': [left.get('declared_text'),
                                        right.get('declared_text')]})
    return conflicts


def _pdf_columns(payload) -> list[str]:
    columns = set()
    for table in (payload.get('tables') or {}).values():
        for column in (table.get('columns') or {}).values():
            if isinstance(column, dict) and column.get('year') is not None:
                columns.add(f"{table.get('table_id')}|{column['year']}|"
                            f"{column.get('indicator') or ''}")
    return sorted(columns)


def _workbook_columns(payload) -> list[str]:
    columns = set()
    for sheet in payload.get('sheets') or []:
        table_id = _workbook_table_id(sheet)
        for column in (sheet.get('columns') or {}).values():
            if column.get('year') is not None:
                columns.add(f"{table_id}|{column['year']}|{column.get('indicator') or ''}")
    return sorted(columns)


def _workbook_table_id(sheet) -> str:
    from .normalize import parse_table_id
    return parse_table_id(sheet.get('sheet') or '') or ''


def _pdf_units(payload) -> dict:
    return {table_id: table.get('unit')
            for table_id, table in (payload.get('tables') or {}).items()}


def _workbook_units(payload) -> dict:
    result = {}
    for sheet in payload.get('sheets') or []:
        result[sheet['sheet']] = next((note['unit'] for note in sheet.get('unit_notes') or []
                                       if note.get('unit')), None)
    return result


def _dedupe_reviews(reviews: list[dict]) -> list[dict]:
    seen, result = set(), []
    for review in reviews:
        identity = review.get('id') or _review_id(
            review.get('kind', ''), (review.get('message'), review.get('detail')))
        review.setdefault('id', identity)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(review)
    return result
