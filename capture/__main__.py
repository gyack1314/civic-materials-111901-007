"""城市能源年鉴采集端命令行入口。

常用流程：
    python -m capture fetch  --url http://127.0.0.1:8765 --run-dir capture-run
    python -m capture extract --run-dir capture-run
    python -m capture review --run-dir capture-run            # 看待核实项
    python -m capture confirm --run-dir capture-run --key ... --evidence ... --note ...
    python -m capture verify --run-dir capture-run
    python -m capture refresh --url http://127.0.0.1:8765 --run-dir capture-run

``fetch``/``refresh`` 只走正常 HTTP 入口；不调用来源的 __admin 控制接口。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .pipeline import (extract, fetch, load_decisions, record_decision,
                       refresh, verify)


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _summary(dataset: dict) -> dict:
    counts = {}
    for record in dataset['records']:
        counts[record['status']] = counts.get(record['status'], 0) + 1
    return {
        'records': len(dataset['records']),
        'status': counts,
        'reviews': len(dataset['reviews']),
        'errata': [document['id'] for document in dataset['errata']],
        'deliverables': {
            'dataset': 'dataset.json',
            'observations': 'observations.csv',
            'reviews': 'reviews.md',
            'report': 'report.html',
        },
    }


def cmd_fetch(args) -> int:
    run_dir = Path(args.run_dir)
    manifest = fetch(args.url, run_dir)
    _print({'run_dir': str(run_dir), 'records': [
        {'id': record['id'], 'corrects': record.get('corrects'),
         'carriers': list(record['carriers'])} for record in manifest['records']]})
    return 0


def cmd_extract(args) -> int:
    dataset = extract(Path(args.run_dir))
    _print(_summary(dataset))
    return 0


def cmd_refresh(args) -> int:
    run_dir = Path(args.run_dir)
    try:
        dataset = refresh(args.url, run_dir)
    except Exception as exc:  # 来源不可达时保留上一次交付，不写出半成品
        previous = run_dir / 'dataset.json'
        if previous.is_file():
            print(f'刷新失败，保留上一次交付：{exc}', file=sys.stderr)
            return 1
        raise
    _print({**_summary(dataset), 'refresh': dataset.get('refresh')})
    return 0


def cmd_review(args) -> int:
    run_dir = Path(args.run_dir)
    dataset = json.loads((run_dir / 'dataset.json').read_text(encoding='utf-8'))
    if args.key:
        record = next((item for item in dataset['records'] if item['key'] == args.key), None)
        if not record:
            print(f'找不到记录 {args.key}', file=sys.stderr)
            return 1
        _print(record)
        return 0
    pending = [record for record in dataset['records']
               if record['status'] in ('pending', 'conflict', 'unavailable')]
    _print({'pending': [{'key': record['key'], 'status': record['status'],
                         'reasons': [reason['message'] for reason in record['pending_reasons']]}
                        for record in pending],
            'reviews': dataset['reviews'],
            'decisions': load_decisions(run_dir)})
    return 0


def cmd_confirm(args) -> int:
    run_dir = Path(args.run_dir)
    if args.action == 'choose' and not args.evidence:
        print('choose 需要 --evidence 指向一条读数证据', file=sys.stderr)
        return 2
    decision = record_decision(run_dir, args.key, args.action, args.evidence,
                               args.note, args.reviewer)
    dataset = extract(run_dir)
    record = next((item for item in dataset['records'] if item['key'] == args.key), None)
    _print({'decision': decision, 'record': record})
    return 0 if record else 1


def cmd_verify(args) -> int:
    problems = verify(Path(args.run_dir))
    if problems:
        _print({'ok': False, 'problems': problems})
        return 1
    _print({'ok': True, 'problems': []})
    return 0


def cmd_decision_file(args) -> int:
    _print(load_decisions(Path(args.run_dir)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m capture',
                                     description='城市能源年鉴跨页表格与勘误采集端')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    fetch_cmd = sub.add_parser('fetch', help='从正常 HTTP 入口获取目录与原件')
    fetch_cmd.add_argument('--url', default='http://127.0.0.1:8765')
    fetch_cmd.add_argument('--run-dir', default='capture-run')
    fetch_cmd.set_defaults(func=cmd_fetch)

    extract_cmd = sub.add_parser('extract', help='解析已获取的原件并生成交付文件')
    extract_cmd.add_argument('--run-dir', default='capture-run')
    extract_cmd.set_defaults(func=cmd_extract)

    refresh_cmd = sub.add_parser('refresh', help='重新获取并采集，保留旧交付快照')
    refresh_cmd.add_argument('--url', default='http://127.0.0.1:8765')
    refresh_cmd.add_argument('--run-dir', default='capture-run')
    refresh_cmd.set_defaults(func=cmd_refresh)

    review_cmd = sub.add_parser('review', help='列出待核实项，或展开单条记录')
    review_cmd.add_argument('--run-dir', default='capture-run')
    review_cmd.add_argument('--key')
    review_cmd.set_defaults(func=cmd_review)

    confirm_cmd = sub.add_parser('confirm', help='记录人工确认或明确保持待核实')
    confirm_cmd.add_argument('--run-dir', default='capture-run')
    confirm_cmd.add_argument('--key', required=True)
    confirm_cmd.add_argument('--action', choices=['choose', 'keep_pending'], required=True)
    confirm_cmd.add_argument('--evidence')
    confirm_cmd.add_argument('--note', default='')
    confirm_cmd.add_argument('--reviewer')
    confirm_cmd.set_defaults(func=cmd_confirm)

    verify_cmd = sub.add_parser('verify', help='检查每条交付值都能回到原件证据')
    verify_cmd.add_argument('--run-dir', default='capture-run')
    verify_cmd.set_defaults(func=cmd_verify)

    decisions_cmd = sub.add_parser('decisions', help='查看已记录的人工确认')
    decisions_cmd.add_argument('--run-dir', default='capture-run')
    decisions_cmd.set_defaults(func=cmd_decision_file)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
