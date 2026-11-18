"""针对采集端的测试。

测试直接读取 data/ 中的真实 PDF 与工作簿（经由来源 HTTP 入口），
不使用任何预先整理好的答案表。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source.server import running_source  # noqa: E402


def run_cli(*args):
    return subprocess.run([sys.executable, '-m', 'capture', *args],
                          cwd=ROOT, capture_output=True, text=True)


class CaptureFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def dataset(self):
        return json.loads((self.run_dir / 'dataset.json').read_text(encoding='utf-8'))

    def record(self, key):
        return next(item for item in self.dataset()['records'] if item['key'] == key)

    def test_fetch_extract_and_verify(self):
        with running_source() as (base, source):
            result = run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_cli('extract', '--run-dir', str(self.run_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
        result = run_cli('verify', '--run-dir', str(self.run_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        dataset = self.dataset()
        keys = {record['key'] for record in dataset['records']}
        self.assertEqual(keys, {'C01|2023|终端能源', 'C01|2024|终端能源',
                                'C02|2023|终端能源', 'C02|2024|终端能源'})
        # 省合计不能被当成城市
        self.assertNotIn('P00', {record['city_code'] for record in dataset['records']})
        self.assertTrue(any(row['city_code'] == 'P00' for row in dataset['excluded']))

    def test_continuation_pages_and_units(self):
        with running_source() as (base, source):
            run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            run_cli('extract', '--run-dir', str(self.run_dir))
        dataset = self.dataset()
        for record in dataset['records']:
            self.assertEqual(record['observation_year'] in (2023, 2024), True)
            self.assertEqual(record['indicator'], '终端能源')
            chosen = record.get('chosen') or {}
            if chosen:
                self.assertEqual(chosen['unit'], '万吨标准煤')

        c02_2023 = self.record('C02|2023|终端能源')
        self.assertEqual(c02_2023['status'], 'adopted')
        self.assertEqual(c02_2023['value_status'], 'zero')
        self.assertEqual(c02_2023['chosen']['numeric_value'], 0)
        self.assertTrue(c02_2023['chosen']['evidence_ids'][0].startswith('pdf:Y-2025.pdf@p2'))
        self.assertEqual(c02_2023['status_meaning'],
                         '原值为 0，表示零值；不是缺报')

        c02_2024 = self.record('C02|2024|终端能源')
        self.assertEqual(c02_2024['status'], 'unavailable')
        self.assertEqual(c02_2024['value_status'], 'missing')

        c01_2024 = self.record('C01|2024|终端能源')
        self.assertEqual(c01_2024['status'], 'conflict')
        self.assertIsNone(c01_2024['chosen'])
        # 脚注不是数值的一部分，但原文与范围说明都要保留
        pdf_reading = next(item for item in c01_2024['readings'] if item['carrier'] == 'pdf')
        self.assertEqual(pdf_reading['raw_value'], '15①')
        self.assertEqual(pdf_reading['numeric_value'], 15)
        self.assertTrue(any('新区' in note['text'] for note in c01_2024['scope_notes']))

    def test_carrier_conflict_keeps_both_evidences(self):
        with running_source() as (base, source):
            run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            run_cli('extract', '--run-dir', str(self.run_dir))
        c01_2024 = self.record('C01|2024|终端能源')
        carriers = {item['carrier']: item for item in c01_2024['readings']}
        self.assertEqual(carriers['pdf']['numeric_value'], 15)
        self.assertEqual(carriers['workbook']['numeric_value'], 16)
        self.assertTrue(carriers['pdf']['citations'][0].startswith('Y-2025.pdf 第 1 页'))
        self.assertEqual(carriers['workbook']['evidence_ids'][0], 'workbook:Y-2025.xlsx#表一!D5')
        review = next(item for item in self.dataset()['reviews']
                      if item['kind'] == 'carrier_conflict')
        self.assertEqual(review['observation_key'], 'C01|2024|终端能源')

    def test_confirm_then_errata_refresh(self):
        with running_source() as (base, source):
            run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            run_cli('extract', '--run-dir', str(self.run_dir))
            result = run_cli('confirm', '--run-dir', str(self.run_dir),
                             '--key', 'C01|2024|终端能源', '--action', 'choose',
                             '--evidence',
                             'pdf:Y-2025.pdf@p1[407.0,235.36,433.94,249.68]',
                             '--note', '按脚注采用', '--reviewer', 'tester')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.record('C01|2024|终端能源')['status'], 'confirmed')

            source.phase = 1
            result = run_cli('refresh', '--url', base, '--run-dir', str(self.run_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['refresh']['new_records'],
                             ['E-2025-1'])
        c01_2024 = self.record('C01|2024|终端能源')
        self.assertEqual(c01_2024['status'], 'corrected')
        self.assertEqual(c01_2024['chosen']['raw_value'], '150000')
        self.assertEqual(c01_2024['chosen']['unit'], '吨标准煤')
        self.assertTrue(c01_2024['chosen']['errata_id'] == 'E-2025-1')
        # 旧读数与人工确认都要保留，且说明被覆盖的原因
        previous = {(item['carrier'], item['raw_value']) for item in
                    c01_2024['chosen']['previous']}
        self.assertEqual(previous, {('pdf', '15①'), ('workbook', '16')})
        self.assertEqual(c01_2024['chosen']['overrides_decision']['reviewer'], 'tester')
        self.assertTrue(any(review['kind'] == 'errata_overrides_decision'
                            for review in self.dataset()['reviews']))
        # 其他记录未因一处更正而撤回
        self.assertEqual(self.record('C01|2023|终端能源')['status'], 'adopted')
        self.assertEqual(self.record('C02|2023|终端能源')['status'], 'adopted')
        # 旧交付快照仍在
        self.assertTrue(list((self.run_dir / 'deliveries').glob('dataset-*.json')))

    def test_verify_detects_tampering(self):
        with running_source() as (base, source):
            run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            run_cli('extract', '--run-dir', str(self.run_dir))
        path = self.run_dir / 'dataset.json'
        dataset = json.loads(path.read_text(encoding='utf-8'))
        for record in dataset['records']:
            if record['key'] == 'C01|2023|终端能源':
                for item in record['readings']:
                    if item['carrier'] == 'workbook':
                        item['raw_value'] = '999'
                        item['value_text'] = '999'
        path.write_text(json.dumps(dataset, ensure_ascii=False), encoding='utf-8')
        result = run_cli('verify', '--run-dir', str(self.run_dir))
        self.assertEqual(result.returncode, 1)
        self.assertIn('工作簿证据单元格与原始值不符', result.stdout)

    def test_deliverables_written(self):
        with running_source() as (base, source):
            run_cli('fetch', '--url', base, '--run-dir', str(self.run_dir))
            run_cli('extract', '--run-dir', str(self.run_dir))
        for name in ('dataset.json', 'observations.csv', 'reviews.md',
                     'report.html', 'manifest.json'):
            self.assertTrue((self.run_dir / name).is_file(), name)
        csv_text = (self.run_dir / 'observations.csv').read_text(encoding='utf-8-sig')
        self.assertIn('城市代码', csv_text)
        self.assertIn('原始值文本', csv_text)
        html = (self.run_dir / 'report.html').read_text(encoding='utf-8')
        self.assertIn('Y-2025.pdf 第 1 页', html)
        self.assertIn('载体读数不一致', html)


if __name__ == '__main__':
    unittest.main()
