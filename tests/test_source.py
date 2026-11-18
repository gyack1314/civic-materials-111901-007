"""本地来源契约检查，不包含尚未实现的采集逻辑。"""
import gzip
import http.client
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from source.server import running_source

class SourceContract(unittest.TestCase):
    def request(self,url,payload=None,headers=None):
        parts=urlsplit(url);conn=http.client.HTTPConnection(parts.hostname,parts.port,timeout=10)
        body=json.dumps(payload).encode() if payload is not None else None
        conn.request('POST' if payload is not None else 'GET',parts.path+('?' + parts.query if parts.query else ''),body,headers or {})
        response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
    def get(self,url,headers=None):return self.request(url,headers=headers)
    def post(self,url,payload):
        status,headers,body=self.request(url,payload)
        self.assertLess(status,300)
        return json.loads(body)
    def test_observable_source(self):
        with running_source() as (base,source):
            self.assertEqual(self.get(base+'/health')[0],200)
            self.post(base+'/__admin/advance',{'seconds':2})
            self.assertEqual(json.loads(self.get(base+'/health')[2])['time'],2)
            self.assertGreaterEqual(len(json.loads(self.get(base+'/__admin/log')[2])),3)

    def test_real_documents(self):
        import fitz
        from openpyxl import load_workbook
        from io import BytesIO
        with running_source() as (base,s):
            pdf=self.get(base+'/files/yearbook.pdf')[2]
            with fitz.open(stream=pdf,filetype='pdf') as doc:
                self.assertEqual(len(doc),2);self.assertIn('2023',doc[0].get_text())
            book=load_workbook(BytesIO(self.get(base+'/files/yearbook.xlsx')[2]));self.assertTrue(book.active.merged_cells.ranges)
            self.post(base+'/__admin/advance',{'phase':1});self.assertEqual(len(json.loads(self.get(base+'/catalog')[2])['records']),2)

if __name__=='__main__':unittest.main()
