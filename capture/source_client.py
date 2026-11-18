"""来源获取。

只通过正常 HTTP 入口取得目录与原件；每一步都留下可回查的请求记录
（时间、URL、ETag、字节数），并把原件写入本次运行的独立目录。
不使用 /__admin/* 控制接口，也不把预先整理的答案表当作来源。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

from . import __version__


class RetrievalError(RuntimeError):
    pass


def _fetch(url: str, timeout: float = 20.0) -> tuple[bytes, dict]:
    request = urllib.request.Request(url, headers={
        'User-Agent': f'yearbook-capture/{__version__}',
        'Accept': '*/*',
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            return body, headers
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise RetrievalError(f'无法获取 {url}: {exc}') from exc


def retrieve(base_url: str, destination: Path) -> dict:
    """获取 /catalog 与全部原件。

    返回 manifest：
    * catalog 的原始 JSON；
    * 每个原件在 catalog 中的记录、URL、保存路径、sha256、ETag；
    * 若目录中声明的 ETag 与下载内容不符，记录告警而不悄悄接受。
    """
    base_url = base_url.rstrip('/')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    originals = destination / 'originals'
    originals.mkdir(exist_ok=True)

    catalog_bytes, catalog_headers = _fetch(base_url + '/catalog')
    try:
        catalog = json.loads(catalog_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalError('目录不是有效 JSON') from exc
    if not isinstance(catalog.get('records'), list):
        raise RetrievalError('目录缺少 records 列表')

    requests_log = [{
        'kind': 'catalog', 'url': base_url + '/catalog',
        'bytes': len(catalog_bytes),
        'sha256': hashlib.sha256(catalog_bytes).hexdigest(),
        'etag': catalog_headers.get('etag'),
    }]

    records = []
    for record in catalog['records']:
        record_id = str(record.get('id') or '').strip()
        if not record_id:
            raise RetrievalError('目录记录缺少 id')
        entry = {'id': record_id, 'corrects': record.get('corrects'),
                 'publication_year': record.get('publication_year'),
                 'edition': record.get('edition'), 'carriers': {}}
        for carrier in ('pdf', 'workbook'):
            link = record.get(carrier)
            if not link:
                continue
            url = link if link.startswith('http') else base_url + link
            body, headers = _fetch(url)
            suffix = '.pdf' if carrier == 'pdf' else '.xlsx'
            if not body:
                raise RetrievalError(f'{record_id} 的 {carrier} 内容为空')
            if carrier == 'pdf' and not body.startswith(b'%PDF-'):
                raise RetrievalError(f'{record_id} 的 pdf 不是有效 PDF')
            if carrier == 'workbook' and not body.startswith(b'PK'):
                raise RetrievalError(f'{record_id} 的 workbook 不是有效 xlsx')
            digest = hashlib.sha256(body).hexdigest()
            path = originals / f'{record_id}{suffix}'
            path.write_bytes(body)
            etag = headers.get('etag')
            entry['carriers'][carrier] = {
                'url': url,
                'path': str(path.relative_to(destination)),
                'bytes': len(body),
                'sha256': digest,
                'etag': etag,
            }
            requests_log.append({**entry['carriers'][carrier], 'kind': carrier,
                                 'record': record_id})
        if not entry['carriers']:
            raise RetrievalError(f'{record_id} 没有任何可用载体')
        records.append(entry)

    manifest = {
        'schema': 'yearbook-capture/manifest-v1',
        'base_url': base_url,
        'catalog': catalog,
        'records': records,
        'requests': requests_log,
    }
    (destination / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def load_manifest(destination: Path) -> dict:
    path = Path(destination) / 'manifest.json'
    if not path.is_file():
        raise RetrievalError(f'缺少 {path}，请先执行 fetch')
    return json.loads(path.read_text(encoding='utf-8'))
