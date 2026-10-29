"""Read-only 1C SOAP/OData -> SQLite. Python 3.9+, no third-party dependencies."""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse as U
import urllib.request as H
import xml.etree.ElementTree as E

ROOT = Path(__file__).resolve().parent
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
WSDL = 'http://schemas.xmlsoap.org/wsdl/'
BIND = 'http://schemas.xmlsoap.org/wsdl/soap/'
STUDY = 'http://sgu-infocom.ru/study'
LIMIT = 64 * 1024 * 1024


class SyncError(Exception):
    pass


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise SyncError('DTD/entities запрещены в XML')
    try:
        return E.fromstring(data)
    except E.ParseError as exc:
        raise SyncError('Ответ не является корректным XML') from exc


class NoRedirect(H.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError('HTTP redirect: укажите конечный URL публикации в конфигурации')


class Client:
    def __init__(self, config):
        self.base = config['base_url'].rstrip('/')
        parsed = U.urlsplit(self.base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SyncError('base_url должен быть HTTP(S) URL публикации без логина, пароля и query')
        self.timeout = float(config.get('timeout_seconds', 15))
        if self.timeout <= 0:
            raise SyncError('timeout_seconds должен быть положительным')
        self.auth = None
        username = os.environ.get('ONEC_USER', '')
        if username:
            token = base64.b64encode((username + ':' + os.environ.get('ONEC_PASSWORD', '')).encode()).decode()
            self.auth = 'Basic ' + token
        context = ssl.create_default_context(cafile=config.get('ca_file') or None)
        self.opener = H.build_opener(H.ProxyHandler({}), H.HTTPSHandler(context=context), NoRedirect())

    def request(self, url, data=None, headers=None):
        if not url.startswith(self.base + '/'):
            raise SyncError('Ссылка ответа выходит за пределы публикации')
        h = dict(headers or {})
        if self.auth:
            h['Authorization'] = self.auth
        req = H.Request(url, data=data, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise SyncError('Ответ больше 64 MiB: уменьшите выборку')
                return raw
        except urllib.error.HTTPError as exc:
            # Do not print response bodies: they can contain personal data.
            hints = {401: 'нужны ONEC_USER/ONEC_PASSWORD; уточните тип аутентификации',
                     403: 'нет прав или доступ запрещён веб-сервером',
                     404: 'проверьте URL и публикацию интерфейса',
                     500: 'ошибка 1С/SOAP; администратору нужны журналы сервера'}
            raise SyncError(f'HTTP {exc.code}: {hints.get(exc.code, "ошибка веб-сервера")}') from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise SyncError('Сетевая/TLS ошибка: проверьте локальную сеть/VPN, адрес, порт, сертификат и firewall') from exc

    def soap(self, filename, operation):
        # Only explicitly reviewed, parameterless read operations are supported.
        allowed = {'study.1cws.xml': {'GetAllCurriculums'},
                   'webapplication.1cws.xml': {'TestConnection'},
                   'RemoteControl.1cws.xml': {'GetCurrentState'}}
        if operation not in allowed.get(filename, set()):
            raise SyncError('SOAP-операция не разрешена')
        wsdl = E.parse(ROOT / filename).getroot()
        ns = wsdl.get('targetNamespace')
        binding = wsdl.find(f'{{{WSDL}}}binding/{{{WSDL}}}operation[@name="{operation}"]/{{{BIND}}}operation')
        if binding is None:
            raise SyncError('Операция не найдена в локальном WSDL')
        env = E.Element(f'{{{SOAP}}}Envelope')
        body = E.SubElement(env, f'{{{SOAP}}}Body')
        E.SubElement(body, f'{{{ns}}}{operation}')
        raw = self.request(self.base + '/ws/' + filename.removesuffix('.xml'),
                           E.tostring(env, encoding='utf-8', xml_declaration=True),
                           {'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': '"' + binding.get('soapAction') + '"'})
        root = xml(raw)
        if root.find(f'.//{{{SOAP}}}Fault') is not None:
            raise SyncError('SOAP Fault: проверьте права и журнал регистрации 1С')
        result = root.find(f'{{{SOAP}}}Body/{{{ns}}}{operation}Response/{{{ns}}}return')
        if result is None:
            raise SyncError('Неожиданная структура SOAP: отсутствует return')
        if any(e.tag.rsplit('}', 1)[-1] == 'Error' for e in result.iter()):
            raise SyncError('1С вернула прикладную Error; снимок не обновлён')
        return result

    def metadata(self):
        root = xml(self.request(self.base + '/odata/standard.odata/$metadata'))
        types = {}
        for schema in root.iter():
            if schema.tag.rsplit('}', 1)[-1] != 'Schema':
                continue
            for child in schema:
                if child.tag.rsplit('}', 1)[-1] == 'EntityType':
                    keys = [p.get('Name') for k in child if k.tag.rsplit('}', 1)[-1] == 'Key' for p in k]
                    types[schema.get('Namespace', '') + '.' + child.get('Name')] = keys
        sets = {e.get('Name'): types.get(e.get('EntityType'), []) for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'EntitySet'}
        if not sets:
            raise SyncError('Нет EntitySet: администратор должен настроить состав OData')
        return sets

    def odata(self, job):
        entity = job['entity']
        sets = self.metadata()
        if entity not in sets or not sets[entity]:
            raise SyncError('Сущность не опубликована или у неё нет поддерживаемого ключа')
        keys = sets[entity]
        size = int(job.get('page_size', 200))
        if size < 1:
            raise SyncError('page_size должен быть положительным')
        params = {'$format': 'json', '$top': str(size), '$orderby': ','.join(keys)}
        if job.get('filter'):
            params['$filter'] = job['filter']
        if job.get('select'):
            params['$select'] = ','.join(dict.fromkeys(keys + job['select']))
        endpoint = self.base + '/odata/standard.odata/' + U.quote(entity, safe='')
        skip, next_url, seen = 0, None, set()
        records = []
        for _ in range(int(job.get('max_pages', 10000))):
            url = next_url or endpoint + '?' + U.urlencode(dict(params, **{'$skip': str(skip)}))
            if url in seen:
                raise SyncError('Зацикленная пагинация')
            seen.add(url)
            try:
                payload = json.loads(self.request(url, headers={'Accept': 'application/json'}))
            except (ValueError, UnicodeError) as exc:
                raise SyncError('OData вернул не JSON') from exc
            if not isinstance(payload, dict):
                raise SyncError('Неожиданный JSON OData')
            data = payload.get('d', payload)
            if not isinstance(data, dict):
                raise SyncError('Неожиданный контейнер OData')
            rows = data.get('value', data.get('results'))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise SyncError('В ответе OData нет списка записей')
            records.extend(rows)
            link = data.get('odata.nextLink') or data.get('@odata.nextLink') or data.get('__next')
            if link:
                next_url = U.urljoin(url, link)
            elif not rows:
                return records, keys
            else:
                # Query until an empty page, even if the server imposes its own page cap.
                skip += len(rows)
                next_url = None
            if link:
                skip += len(rows)
        raise SyncError('Превышен max_pages: неполная выборка не сохранена')


def value(element):
    if element.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in ('true', '1'):
        return None
    if not len(element):
        return element.text or ''
    result = {}
    for child in element:
        name = child.tag.rsplit('}', 1)[-1]
        v = value(child)
        if name in result:
            raise SyncError('Неожиданные повторяющиеся поля SOAP')
        result[name] = v
    return result


def database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS records (
      dataset TEXT NOT NULL, record_key TEXT NOT NULL, payload TEXT NOT NULL,
      active INTEGER NOT NULL, updated_at TEXT NOT NULL, seen_at TEXT NOT NULL,
      PRIMARY KEY(dataset, record_key));
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER, error TEXT);
    CREATE VIEW IF NOT EXISTS current_records AS SELECT * FROM records WHERE active=1;
    ''')
    return db


def now():
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(db, dataset, rows, keys, allow_empty=False):
    if not rows and not allow_empty:
        raise SyncError('Пустая выборка: сохранение запрещено; проверьте права/фильтр или задайте allow_empty=true')
    prepared, seen = [], set()
    timestamp = now()
    for row in rows:
        if any(k not in row or row[k] is None or row[k] == '' for k in keys):
            raise SyncError('В записи отсутствует ключ')
        key = json.dumps([row[k] for k in keys], ensure_ascii=False, sort_keys=True)
        if key in seen:
            raise SyncError('Дубли ключей: нестабильная пагинация или неверный ключ')
        seen.add(key)
        prepared.append((dataset, key, json.dumps(row, ensure_ascii=False, sort_keys=True), timestamp, timestamp))
    with db:
        db.execute('UPDATE records SET active=0 WHERE dataset=?', (dataset,))
        db.executemany('''INSERT INTO records VALUES (?, ?, ?, 1, ?, ?)
          ON CONFLICT(dataset,record_key) DO UPDATE SET
          updated_at=CASE WHEN records.payload != excluded.payload THEN excluded.updated_at ELSE records.updated_at END,
          payload=excluded.payload, active=1, seen_at=excluded.seen_at''', prepared)


def sync(client, db, config):
    failed = False
    for job in config['jobs']:
        start = now()
        signature = json.dumps({'base': client.base, 'job': job}, sort_keys=True)
        dataset = job['name'] + ':' + hashlib.sha256(signature.encode()).hexdigest()[:16]
        try:
            if job['kind'] == 'soap_curriculums':
                result = client.soap('study.1cws.xml', 'GetAllCurriculums')
                if any(e.tag != f'{{{STUDY}}}Curriculum' for e in result):
                    raise SyncError('Неожиданные поля списка учебных планов')
                rows = [value(e) for e in result]
                keys = ['CurriculumId']
            elif job['kind'] == 'odata':
                rows, keys = client.odata(job)
            else:
                raise SyncError('Неизвестный kind задания')
            save_snapshot(db, dataset, rows, keys, job.get('allow_empty', False))
            status, error, count = 'ok', None, len(rows)
            print(f'{job["name"]}: OK, записей {count}', flush=True)
        except SyncError as exc:
            failed = True
            status, error, count = 'error', str(exc), None
            print(f'{job["name"]}: {exc}', file=sys.stderr, flush=True)
        with db:
            db.execute('INSERT INTO runs(dataset,started_at,finished_at,status,row_count,error) VALUES (?,?,?,?,?,?)',
                       (dataset, start, now(), status, count, error))
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    parser.add_argument('command', choices=['diagnose', 'entities', 'sync', 'watch', 'status'])
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
        client = Client(config)
        if args.command == 'entities':
            print(json.dumps(client.metadata(), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'diagnose':
            checks = [('SOAP WSDL', lambda: xml(client.request(client.base + '/ws/study.1cws?wsdl'))),
                      ('SOAP TestConnection', lambda: client.soap('webapplication.1cws.xml', 'TestConnection').text),
                      ('OData metadata', lambda: len(client.metadata()))]
            failed = False
            for name, check in checks:
                try:
                    result = check()
                    print(f'{name}: OK' + (f' ({result})' if isinstance(result, (str, int)) else ''))
                except SyncError as exc:
                    failed = True
                    print(f'{name}: {exc}')
            print('Доступность интерфейса не доказывает право читать нужные данные. Следующий шаг: sync.')
            return int(failed)
        dbpath = Path(config.get('database', 'data/mirror.sqlite3'))
        if not dbpath.is_absolute():
            dbpath = config_path.parent / dbpath
        # Connection.__exit__ commits/rolls back, but does not close the file.
        # Explicitly close it on returns, errors and interruption (also on Windows).
        with closing(database(dbpath)) as db:
            if args.command == 'status':
                print(json.dumps({'database': str(dbpath), 'datasets': db.execute(
                    'SELECT dataset, count(*), sum(active) FROM records GROUP BY dataset').fetchall(),
                    'last_runs': db.execute('SELECT dataset,finished_at,status,row_count,error FROM runs ORDER BY id DESC LIMIT 10').fetchall()}, ensure_ascii=False, indent=2))
                return 0
            jobs = config['jobs']
            if not jobs or len({j['name'] for j in jobs}) != len(jobs):
                raise SyncError('Нужен непустой список jobs с уникальными name')
            interval = float(config.get('interval_seconds', 300))
            if interval < 1:
                raise SyncError('interval_seconds должен быть >= 1')
            while True:
                result = sync(client, db, config)
                if args.command == 'sync':
                    return result
                time.sleep(interval)
    except (SyncError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Остановлено')
        return 0


if __name__ == '__main__':
    sys.exit(main())
