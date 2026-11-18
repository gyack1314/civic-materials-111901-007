"""采集流水线：获取 → 解析 → 合并 → 交付文件。

一次运行的全部产物放在同一个运行目录，原件只读保存，解析结果可重建；
勘误到达后 ``refresh`` 只重新解析受影响的原件，并把旧交付存为快照。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import __version__
from .errata import parse_errata
from .model import assemble, evidence_id
from .source_client import load_manifest, retrieve


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def _json_read(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def fetch(base_url: str, run_dir: Path) -> dict:
    manifest = retrieve(base_url, run_dir)
    _json_write(Path(run_dir) / 'manifest.json', manifest)
    return manifest


def decisions_path(run_dir: Path) -> Path:
    return Path(run_dir) / 'decisions.json'


def load_decisions(run_dir: Path) -> dict:
    return _json_read(decisions_path(run_dir), {'decisions': {}})


def record_decision(run_dir: Path, key: str, action: str, evidence: str | None,
                    note: str, reviewer: str | None) -> dict:
    store = load_decisions(run_dir)
    decision = {
        'action': action,
        'evidence_id': evidence,
        'note': note,
        'reviewer': reviewer,
        'decided_at': _now(),
    }
    store.setdefault('decisions', {})[key] = decision
    _json_write(decisions_path(run_dir), store)
    return decision


def extract(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    manifest['_destination'] = str(run_dir)
    dataset = _assemble_from(manifest, run_dir)
    prior = _json_read(run_dir / 'dataset.json')
    if prior:
        # 每次重写交付前先留档：人工确认、勘误重建都不会让旧提取记录失去解释。
        _archive_delivery(run_dir, prior)
        dataset = _carry_forward(dataset, prior)
    _check_fetch_coverage(run_dir, manifest, dataset)
    _write_dataset(run_dir, dataset)
    return dataset


def _check_fetch_coverage(run_dir: Path, manifest: dict, dataset: dict) -> None:
    """目录与本地原件不一致时给出可解释的告警，而不是安静地产出半份数据。"""
    fetched = {record['id'] for record in manifest.get('records', [])}
    for past in sorted((run_dir / 'deliveries').glob('dataset-*.json')):
        try:
            old = json.loads(past.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        old_ids = {record.get('id') for record in
                   (old.get('source') or {}).get('records', [])}
        if old_ids - fetched:
            dataset.setdefault('notices', []).append({
                'kind': 'source_record_missing',
                'message': '目录不再包含上次交付使用的原件，旧交付仍保留在 '
                           + past.name + '；本次只采集当前目录中的原件',
                'records': sorted(old_ids - fetched),
            })
            break


def _assemble_from(manifest: dict, run_dir: Path) -> dict:
    from .model import load_parsed
    parsed = load_parsed(manifest)
    errata = parse_errata(manifest, run_dir)
    decisions = load_decisions(run_dir)
    dataset = assemble(parsed, errata, decisions)
    dataset['schema'] = 'yearbook-capture/dataset-v1'
    dataset['capture_version'] = __version__
    dataset['generated_at'] = _now()
    dataset['source'] = {
        'base_url': manifest.get('base_url'),
        'records': manifest.get('records'),
    }
    dataset['declarations'] = _declarations(parsed)
    dataset['evidence_index'] = _evidence_index(dataset)
    return dataset


def _write_dataset(run_dir: Path, dataset: dict) -> None:
    _json_write(run_dir / 'dataset.json', dataset)
    _json_write(run_dir / 'reviews.json', dataset['reviews'])
    _write_deliverables(run_dir, dataset)


def refresh(base_url: str, run_dir: Path) -> dict:
    """获取完整目录后重新采集；只解析当前目录中实际存在且未变动的原件。

    勘误到达时年鉴与勘误都会在目录中，旧年鉴的已提取记录保持不变；
    旧交付先存档，新交付里标出变化。来源不可达时保留上一次交付。
    """
    run_dir = Path(run_dir)
    prior_manifest = _json_read(run_dir / 'manifest.json')
    manifest = retrieve(base_url, run_dir)
    manifest['_destination'] = str(run_dir)
    _json_write(run_dir / 'manifest.json', manifest)
    dataset = _assemble_from(manifest, run_dir)
    previous = _json_read(run_dir / 'dataset.json')
    if previous:
        _archive_delivery(run_dir, previous)
        dataset = _carry_forward(dataset, previous)
    before = {record['id'] for record in (prior_manifest or {}).get('records', [])}
    after = {record['id'] for record in manifest['records']}
    dataset['refresh'] = {
        'new_records': sorted(after - before),
        'unchanged_records': sorted(after & before),
    }
    _write_dataset(run_dir, dataset)
    return dataset


def _verify_original_text(run_dir: Path, dataset: dict, manifest: dict) -> list[dict]:
    """回到原件重新读取每个读数位置，核对原始值文本确实位于该位置。

    这是"可回查"的可执行检查，不只是比对本地 JSON 内部一致。
    """
    import fitz
    from openpyxl import load_workbook
    from io import BytesIO

    documents = {}
    for record in manifest.get('records', []):
        for carrier, spec in (record.get('carriers') or {}).items():
            path = Path(spec['path'])
            if not path.is_absolute():
                path = run_dir / path
            if path.is_file():
                documents[path.name] = {'carrier': carrier, 'bytes': path.read_bytes()}
    problems = []
    pdf_cache, workbook_cache = {}, {}
    checked = []
    for record in dataset['records']:
        for item in record.get('readings') or []:
            checked.append((record['key'], item))
        chosen = record.get('chosen')
        if chosen:
            checked.append((record['key'], chosen))
    for key, item in checked:
        for evidence in item.get('evidence') or []:
            document = evidence.get('document')
            raw = item.get('raw_value')
            if not raw or document not in documents:
                continue
            if evidence.get('carrier') == 'pdf':
                if document not in pdf_cache:
                    pdf_cache[document] = fitz.open(
                        stream=documents[document]['bytes'], filetype='pdf')
                text = pdf_cache[document][evidence['page'] - 1].get_textbox(
                    fitz.Rect(*evidence['rect']))
                if item.get('value_text') and item['value_text'] not in text:
                    problems.append({
                        'key': key, 'problem': 'PDF 证据位置读不到原始值',
                        'evidence': evidence,
                        'expected': item.get('value_text'),
                        'found': text,
                    })
            elif evidence.get('carrier') == 'workbook':
                if document not in workbook_cache:
                    workbook_cache[document] = load_workbook(
                        BytesIO(documents[document]['bytes']), data_only=True)
                sheet = workbook_cache[document][evidence['sheet']]
                value = sheet[evidence['cell']].value
                if item.get('value_text') and str(value).strip() != item.get('value_text').strip():
                    problems.append({
                        'key': key, 'problem': '工作簿证据单元格与原始值不符',
                        'evidence': evidence,
                        'expected': item.get('value_text'),
                        'found': value,
                    })
    for pdf in pdf_cache.values():
        pdf.close()
    return problems


def _declarations(parsed: dict) -> dict:
    result = {}
    for record_id, record in parsed['records'].items():
        result[record_id] = record.get('declared')
    return result


def _evidence_index(dataset: dict) -> dict:
    index = {}
    for record in dataset['records']:
        for item in record.get('readings', []):
            for evidence in item.get('evidence', []):
                index.setdefault(evidence_id(evidence), {
                    'carrier': evidence.get('carrier'),
                    'document': evidence.get('document'),
                    'page': evidence.get('page'),
                    'rect': evidence.get('rect'),
                    'sheet': evidence.get('sheet'),
                    'cell': evidence.get('cell'),
                    'records': [],
                })['records'].append(record['key'])
        chosen = record.get('chosen')
        if chosen:
            for evidence_id_value in chosen.get('evidence_ids') or []:
                index.setdefault(evidence_id_value, {'records': []})
    return index


def _archive_delivery(run_dir: Path, prior: dict) -> None:
    stamp = prior.get('generated_at', 'previous').replace(':', '').replace('-', '')
    target = run_dir / 'deliveries' / f'dataset-{stamp}.json'
    if not target.exists():
        _json_write(target, prior)


def _carry_forward(dataset: dict, prior: dict) -> dict:
    """刷新时不抹掉旧交付：新记录带上它替换掉的旧读数与旧状态。"""
    previous = {record['key']: record for record in prior.get('records', [])}
    for record in dataset['records']:
        old = previous.get(record['key'])
        if not old:
            record['history'] = []
            continue
        if old.get('chosen') != record.get('chosen') or old.get('status') != record.get('status'):
            record.setdefault('history', []).append({
                'generated_at': prior.get('generated_at'),
                'status': old.get('status'),
                'chosen': old.get('chosen'),
                'reason': '重新采集后发生变化；旧交付仍保存在 deliveries/ 快照中',
            })
        else:
            record.setdefault('history', old.get('history', []))
    return dataset


def _write_deliverables(run_dir: Path, dataset: dict) -> None:
    from .export import write_csv, write_html, write_review_markdown
    write_csv(run_dir / 'observations.csv', dataset['records'])
    write_review_markdown(run_dir / 'reviews.md', dataset)
    write_html(run_dir / 'report.html', dataset)


def verify(run_dir: Path, check_originals: bool = True) -> list[dict]:
    """针对性检查：每条交付值都必须能回到原件证据，且状态与原文一致。"""
    run_dir = Path(run_dir)
    dataset = _json_read(run_dir / 'dataset.json')
    if not dataset:
        raise SystemExit('缺少 dataset.json，请先执行 extract')
    manifest = _json_read(run_dir / 'manifest.json')
    problems = []
    if check_originals:
        problems.extend(_verify_original_text(run_dir, dataset, manifest or {}))
    for record in dataset['records']:
        if not record.get('readings'):
            problems.append({'key': record['key'], 'problem': '无任何载体读数'})
            continue
        for item in record['readings']:
            if not item.get('evidence_ids'):
                problems.append({'key': record['key'], 'problem': '读数缺少证据位置'})
            if item.get('value_status') in ('value', 'zero') and item.get('numeric_value') is None:
                problems.append({'key': record['key'], 'problem': '可用读数缺少数值'})
        chosen = record.get('chosen')
        if record['status'] in ('adopted', 'confirmed', 'corrected'):
            if not chosen:
                problems.append({'key': record['key'], 'problem': '已采用记录缺少 chosen'})
                continue
            if not chosen.get('unit'):
                problems.append({'key': record['key'], 'problem': '已采用记录缺少单位'})
            if not chosen.get('evidence_ids'):
                problems.append({'key': record['key'], 'problem': '已采用记录缺少证据'})
            if chosen.get('raw_value') is None:
                problems.append({'key': record['key'], 'problem': '已采用记录缺少原始值文本'})
            if not chosen.get('citations'):
                problems.append({'key': record['key'], 'problem': '已采用记录缺少可读引用'})
        if record['status'] in ('pending', 'conflict'):
            if not record.get('pending_review_ids') and not any(
                    reason.get('review_id') for reason in record.get('pending_reasons', [])):
                problems.append({'key': record['key'], 'problem': '待核实记录没有对应核实项'})
    return problems
