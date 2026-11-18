"""解析层检查：几何分页、符号语义、行角色。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capture.normalize import classify_row, parse_value, strip_footnote_markers  # noqa: E402
from capture.pdf_parser import build_page_tables  # noqa: E402


def cell(text, bbox=None):
    return {'texts': [text], 'bboxes': [bbox or [0, 0, 10, 10]],
            'bbox': bbox or [0, 0, 10, 10], 'spans': []}


def page(number, title, rows, bands):
    return {'page': number, 'table': title, 'rows': rows, 'bands': bands,
            'leafs': [{'index': 0, 'x0': 0, 'x1': 10},
                      {'index': 1, 'x0': 10, 'x1': 20},
                      {'index': 2, 'x0': 20, 'x1': 30},
                      {'index': 3, 'x0': 30, 'x1': 40}]}


def header_page(number, table_id, years, continuation=False):
    """表头：城市代码/城市两列 + 跨两列的合并表头 + 两个年份列。"""
    title = {'table_id': table_id, 'indicator': '终端能源', 'unit': '万吨标准煤',
             'continuation': continuation, 'text': table_id,
             'sources': [{'text': table_id, 'bbox': [0, 0, 10, 10]}],
             'bbox': [0, 0, 10, 10]}
    rows = {
        0: {(0, 0): cell('城市代码'), (1, 1): cell('城市'),
            (2, 3): cell('终端能源')},
        1: {(2, 2): cell(years[0]), (3, 3): cell(years[1])},
    }
    return page(number, title, rows, [(0, 10), (10, 20), (20, 30), (30, 40)])


def data_row(code, name, value):
    return {(0, 0): cell(code), (1, 1): cell(name), (2, 2): cell(value)}


class ValueSemantics(unittest.TestCase):
    def test_symbols_keep_their_meaning(self):
        legend = {'0': '零值', '': '未刊载', '—': '不适用', '⋯': '缺报'}
        self.assertEqual(parse_value('0', legend)['value_status'], 'zero')
        self.assertEqual(parse_value('0', legend)['numeric_value'], 0)
        self.assertEqual(parse_value('', legend)['value_status'], 'not_published')
        self.assertEqual(parse_value('—', legend)['value_status'], 'not_applicable')
        self.assertEqual(parse_value('⋯', legend)['value_status'], 'missing')
        self.assertIsNone(parse_value('⋯', legend)['numeric_value'])

    def test_footnote_is_not_part_of_number(self):
        value = parse_value('15①')
        self.assertEqual(value['raw_value'], '15①')
        self.assertEqual(value['numeric_value'], 15)
        self.assertEqual(value['footnote_markers'], ['①'])
        self.assertEqual(strip_footnote_markers('3[2]'), ('3', ['[2]']))

    def test_unknown_text_is_not_guessed(self):
        value = parse_value('约 12')
        self.assertEqual(value['value_status'], 'unparsed')
        self.assertTrue(value['unparsed'])
        self.assertIsNone(value['numeric_value'])

    def test_row_roles(self):
        self.assertEqual(classify_row('P00', '省合计')['role'], 'province_total')
        self.assertEqual(classify_row('P00', '某某地区')['role'], 'province_total')
        self.assertEqual(classify_row('C01', '城甲')['role'], 'city')
        self.assertEqual(classify_row('C09', '合计')['role'], 'subtotal')
        self.assertEqual(classify_row('', '城丁')['role'], 'unclassified')


class ContinuationSafety(unittest.TestCase):
    def test_matching_continuation_is_used(self):
        first = header_page(1, '表一', ['2023', '2024'])
        first['rows'][2] = data_row('C01', '城甲', '12')
        second = header_page(2, '表一', ['2023', '2024'], continuation=True)
        second['rows'][2] = data_row('C02', '城乙', '0')
        result = build_page_tables({'document': 'x.pdf', 'pages': [first, second],
                                    'legend': {}, 'footnotes': []})
        self.assertEqual({(o['city_code'], o['observation_year'])
                          for o in result['observations']},
                         {('C01', 2023), ('C01', 2024),
                          ('C02', 2023), ('C02', 2024)})
        self.assertTrue(all(o['unit'] == '万吨标准煤'
                            for o in result['observations']))

    def test_year_mismatch_on_continuation_does_not_reuse_previous_years(self):
        first = header_page(1, '表一', ['2023', '2024'])
        first['rows'][2] = data_row('C01', '城甲', '12')
        second = header_page(2, '表一', ['2021', '2022'], continuation=True)
        second['rows'][2] = data_row('C02', '城乙', '0')
        result = build_page_tables({'document': 'x.pdf', 'pages': [first, second],
                                    'legend': {}, 'footnotes': []})
        self.assertEqual({o['city_code'] for o in result['observations']}, {'C01'})
        self.assertTrue(any(review['kind'] == 'continuation_uncertain'
                            for review in result['reviews']))
        self.assertTrue(any(row['role'] == 'column_mismatch' for row in result['excluded']))

    def test_continuation_without_previous_page_is_not_attached(self):
        second = header_page(2, '表一', ['2023', '2024'], continuation=True)
        second['rows'][2] = data_row('C02', '城乙', '0')
        result = build_page_tables({'document': 'x.pdf', 'pages': [second],
                                    'legend': {}, 'footnotes': []})
        self.assertEqual(result['observations'], [])
        self.assertTrue(any(review['kind'] == 'continuation_uncertain'
                            for review in result['reviews']))

    def test_province_total_is_excluded(self):
        first = header_page(1, '表一', ['2023', '2024'])
        first['rows'][2] = data_row('P00', '省合计', '1000')
        result = build_page_tables({'document': 'x.pdf', 'pages': [first],
                                    'legend': {}, 'footnotes': []})
        self.assertEqual(result['observations'], [])
        self.assertEqual(result['excluded'][0]['city_code'], 'P00')


if __name__ == '__main__':
    unittest.main()
