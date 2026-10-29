import base64
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import xml.etree.ElementTree as E

import sync1c as s

METADATA = b'''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices><Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="Test"><EntityType Name="Item"><Key><PropertyRef Name="Ref_Key"/></Key></EntityType><EntityContainer Name="Container"><EntitySet Name="Catalog_Demo" EntityType="Test.Item"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'''


class Handler(BaseHTTPRequestHandler):
    rows = [{'Ref_Key': 'a', 'Description': 'A'}, {'Ref_Key': 'b', 'Description': 'Б'}, {'Ref_Key': 'c', 'Description': 'C'}]
    failure = False
    next_link = False
    unauthorized = False
    soap_fault = False

    def log_message(self, *args):
        pass

    def send(self, body, status=200):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth(self):
        expected = 'Basic ' + base64.b64encode(b'test:secret').decode()
        if self.unauthorized or self.headers.get('Authorization') != expected:
            self.send(b'Unauthorized', 401)
            return False
        return True

    def do_GET(self):
        if not self.auth():
            return
        parsed = urlsplit(self.path)
        if parsed.path.endswith('$metadata'):
            self.send(METADATA)
            return
        if parsed.query == 'wsdl':
            self.send((s.ROOT / 'study.1cws.xml').read_bytes())
            return
        query = parse_qs(parsed.query)
        skip = int(query.get('$skip', ['0'])[0])
        if self.failure and skip:
            self.send(b'server error', 500)
            return
        # Server caps page size to one even if the client asks for 200.
        result = {'value': self.rows[skip:skip + 1]}
        if self.next_link and skip + 1 < len(self.rows):
            result['odata.nextLink'] = 'Catalog_Demo?$skip=' + str(skip + 1)
        self.send(json.dumps(result).encode())

    def do_POST(self):
        if not self.auth():
            return
        body = self.rfile.read(int(self.headers['Content-Length']))
        root = E.fromstring(body)
        op = list(root.find(f'{{{s.SOAP}}}Body'))[0]
        if op.tag == f'{{{s.STUDY}}}GetAllCurriculums':
            if self.headers['SOAPAction'] != '"http://sgu-infocom.ru/study#WebStudy:GetAllCurriculums"':
                self.send(b'Bad action', 400)
                return
            response = '<GetAllCurriculumsResponse xmlns="http://sgu-infocom.ru/study"><return><Curriculum><CurriculumId>42</CurriculumId><CurriculumName>Test</CurriculumName><CurriculumRef><ReferenceId>42</ReferenceId><ReferenceUID>uid</ReferenceUID><ReferenceName>Test</ReferenceName><ReferenceClassName>Curriculum</ReferenceClassName></CurriculumRef><SpecialtyName>IT</SpecialtyName><FormsOfEducationName>Full</FormsOfEducationName><DepartmentName>IT</DepartmentName><LevelOfEducationName>BA</LevelOfEducationName></Curriculum></return></GetAllCurriculumsResponse>'
        else:
            response = '<TestConnectionResponse xmlns="http://sgu-infocom.ru/application"><return>true</return></TestConnectionResponse>'
        if self.soap_fault:
            response = '<s:Fault><faultcode>s:Server</faultcode><faultstring>Failure</faultstring></s:Fault>'
        self.send(('<s:Envelope xmlns:s="' + s.SOAP + '"><s:Body>' + response + '</s:Body></s:Envelope>').encode())


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, ONEC_USER='test', ONEC_PASSWORD='secret')
        self.env.start()
        self.addCleanup(self.env.stop)
        Handler.failure = Handler.next_link = Handler.unauthorized = Handler.soap_fault = False
        self.config = {'base_url': f'http://127.0.0.1:{self.server.server_port}/testun',
                       'jobs': [{'name': 'demo', 'kind': 'odata', 'entity': 'Catalog_Demo'}]}
        self.client = s.Client(self.config)
        self.db = s.database(Path(self.temp.name) / 'mirror.sqlite3')
        self.addCleanup(self.db.close)

    def run_sync(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return s.sync(self.client, self.db, self.config)

    def test_capped_pages_and_idempotence(self):
        self.assertEqual(self.run_sync(), 0)
        before = self.db.execute('SELECT record_key,updated_at FROM records ORDER BY record_key').fetchall()
        self.assertEqual(len(before), 3)
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(before, self.db.execute('SELECT record_key,updated_at FROM records ORDER BY record_key').fetchall())

    def test_next_links(self):
        Handler.next_link = True
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM current_records').fetchone()[0], 3)

    def test_failure_keeps_snapshot_and_logs_error(self):
        self.run_sync()
        before = self.db.execute('SELECT * FROM records').fetchall()
        Handler.failure = True
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(before, self.db.execute('SELECT * FROM records').fetchall())
        self.assertEqual(self.db.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'error')

    def test_soap_real_wsdl_and_http(self):
        self.config['jobs'] = [{'name': 'plans', 'kind': 'soap_curriculums'}]
        self.assertEqual(self.run_sync(), 0)
        row = json.loads(self.db.execute('SELECT payload FROM records').fetchone()[0])
        self.assertEqual(row['CurriculumId'], '42')
        self.assertEqual(row['CurriculumRef']['ReferenceUID'], 'uid')

    def test_auth_failure_and_fault(self):
        Handler.unauthorized = True
        with self.assertRaisesRegex(s.SyncError, '401'):
            self.client.metadata()
        Handler.unauthorized = False
        Handler.soap_fault = True
        with self.assertRaisesRegex(s.SyncError, 'SOAP Fault'):
            self.client.soap('study.1cws.xml', 'GetAllCurriculums')

    def test_snapshot_update_and_missing_records(self):
        s.save_snapshot(self.db, 'x', [{'id': 1, 'name': 'old'}, {'id': 2}], ['id'])
        s.save_snapshot(self.db, 'x', [{'id': 1, 'name': 'new'}], ['id'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM current_records').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM records').fetchone()[0], 2)
        self.assertIn('new', self.db.execute('SELECT payload FROM current_records').fetchone()[0])

    def test_empty_duplicate_and_missing_key_protect_snapshot(self):
        s.save_snapshot(self.db, 'x', [{'id': 'a'}], ['id'])
        for rows in ([], [{'id': 'a'}, {'id': 'a'}], [{'name': 'bad'}]):
            with self.assertRaises(s.SyncError):
                s.save_snapshot(self.db, 'x', rows, ['id'])
            self.assertEqual(self.db.execute('SELECT count(*) FROM current_records').fetchone()[0], 1)
        s.save_snapshot(self.db, 'x', [], ['id'], allow_empty=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM current_records').fetchone()[0], 0)

    def test_outside_publication_and_mutation_blocked(self):
        with self.assertRaises(s.SyncError):
            self.client.request('http://example.com/data')
        with self.assertRaises(s.SyncError):
            self.client.soap('study.1cws.xml', 'SaveMark')

    def test_limit_and_bad_payload(self):
        with self.assertRaisesRegex(s.SyncError, 'max_pages'):
            self.client.odata({'entity': 'Catalog_Demo', 'max_pages': 1})
        with patch.object(self.client, 'request', return_value=b'{"error": "failed"}'):
            with patch.object(self.client, 'metadata', return_value={'Catalog_Demo': ['Ref_Key']}):
                with self.assertRaises(s.SyncError):
                    self.client.odata({'entity': 'Catalog_Demo'})

    def test_cli_workflow(self):
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps(self.config))
        for command in ('diagnose', 'entities', 'sync', 'status'):
            with patch.object(s.sys, 'argv', ['sync1c.py', '--config', str(path), command]), redirect_stdout(io.StringIO()):
                self.assertEqual(s.main(), 0)
        self.assertTrue((path.parent / 'data/mirror.sqlite3').exists())

    def test_cli_closes_database_on_return_error_and_interrupt(self):
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps(self.config))
        for command, effect, expected in (
            ('status', None, 0),
            ('sync', None, 0),
            ('sync', s.SyncError('test failure'), 1),
            ('watch', KeyboardInterrupt(), 0),
        ):
            with self.subTest(command=command, effect=type(effect).__name__):
                connection = s.database(Path(self.temp.name) / 'cli.sqlite3')
                self.addCleanup(connection.close)
                with patch.object(s, 'database', return_value=connection), \
                     patch.object(s, 'sync', return_value=0, side_effect=effect), \
                     patch.object(s.sys, 'argv', ['sync1c.py', '--config', str(path), command]), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(s.main(), expected)
                # Keep a strong reference: garbage collection must not hide a leak.
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
