"""Local image task ledger. No network/model/API calls; the host draws images."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import tempfile
import uuid

from PIL import Image

SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY COLLATE NOCASE, filename TEXT NOT NULL UNIQUE COLLATE NOCASE,
 mode TEXT NOT NULL CHECK(mode IN ('generate','edit','external')),prompt TEXT NOT NULL,
 refs TEXT NOT NULL,inputs TEXT NOT NULL,definition TEXT NOT NULL,parent TEXT,
 family TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>0),
 state TEXT NOT NULL CHECK(state IN ('pending','dispatched','uncertain','completed','needs_review','waiting_external','failed')),
 sha256 TEXT,width INTEGER,height INTEGER,note TEXT);
CREATE TABLE batches(id TEXT PRIMARY KEY,limit_calls INTEGER NOT NULL CHECK(limit_calls>0),items TEXT NOT NULL);
CREATE TABLE batch_items(batch TEXT NOT NULL REFERENCES batches(id),task TEXT NOT NULL REFERENCES tasks(id),
 position INTEGER NOT NULL,PRIMARY KEY(batch,task));
CREATE TABLE jobs(token TEXT PRIMARY KEY,task TEXT NOT NULL REFERENCES tasks(id),batch TEXT NOT NULL REFERENCES batches(id),
 phase TEXT NOT NULL CHECK(phase IN ('dispatched','uncertain','completed','failed')),refs TEXT NOT NULL,note TEXT);
CREATE INDEX jobs_batch ON jobs(batch);
CREATE TABLE requests(id TEXT PRIMARY KEY,definition TEXT NOT NULL,result TEXT NOT NULL);
INSERT INTO meta VALUES('schema','story-to-image-2');
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('Use a stable ASCII task/request ID, 1..100 characters')
    return value


def relative(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('A relative filename/path is required')
    value = value.replace('\\', '/')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith(('.', ' ')) or re.search(r'[\x00-\x1f:<>"|?*]', p)
           or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p) for p in parts):
        raise ValueError('Invalid or non-portable relative path: ' + value)
    return str(PurePosixPath(value))


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'state.sqlite3'
        if not self.path.is_file():
            raise ValueError('Missing state.sqlite3; initialize a new project or restore the original ledger')
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        if self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] != 'story-to-image-2':
            self.close()
            raise ValueError('Unsupported ledger schema; preserve it')

    @classmethod
    def init(cls, root):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'state.sqlite3'
        if not path.exists():
            if any((root / name).exists() for name in ('progress.json', 'output')):
                raise ValueError('Existing/legacy project data; choose a new directory or restore its original state')
            # Exclusive creation prevents two initializers from replacing each other's state.
            with path.open('xb'):
                pass
            db = sqlite3.connect(path)
            try:
                db.executescript(SCHEMA)
                db.commit()
            finally:
                db.close()
        return cls(root)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def inside(self, name):
        path = (self.root / relative(name)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Path escapes project (including symlinks): ' + name)
        return path

    def task(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['id'] != task_id:
            raise ValueError('Unknown task ID (case-sensitive): ' + str(task_id))
        return dict(row)

    def output(self, task):
        return self.inside('output/' + task['filename'])

    def verify(self, task):
        path = self.output(task)
        if not task['sha256'] or not path.is_file() or sha(path) != task['sha256']:
            raise ValueError('Saved image missing/changed; restore original, do not redraw: ' + task['id'])
        return path

    def import_plan(self, document):
        if set(document) != {'tasks'} or not isinstance(document['tasks'], list) or not document['tasks']:
            raise ValueError('Plan must have a non-empty tasks array')
        with self.transaction():
            existing = {r['id']: dict(r) for r in self.db.execute('SELECT * FROM tasks')}
            normalized, ids, names = [], set(), set()
            for raw in document['tasks']:
                if not isinstance(raw, dict) or set(raw) - {'id', 'filename', 'mode', 'prompt', 'refs', 'inputs'}:
                    raise ValueError('Unsupported task fields')
                item = dict(raw)
                item['id'] = identifier(item.get('id'))
                item['filename'] = relative(item.get('filename'))
                if not item['filename'].lower().endswith('.png'):
                    raise ValueError('Output filenames must end with .png')
                if item.get('mode') not in ('generate', 'edit', 'external'):
                    raise ValueError('mode must be generate, edit or external')
                item.setdefault('prompt', '')
                if not isinstance(item['prompt'], str) or (item['mode'] != 'external' and not item['prompt'].strip()):
                    raise ValueError('A complete non-empty image prompt is required')
                for field in ('refs', 'inputs'):
                    item.setdefault(field, [])
                    if not isinstance(item[field], list) or any(not isinstance(v, str) for v in item[field]):
                        raise ValueError(field + ' must be an array of strings')
                item['inputs'] = [relative(v) for v in item['inputs']]
                if item['id'].casefold() in ids or item['filename'].casefold() in names:
                    raise ValueError('Duplicate ID/filename in plan')
                ids.add(item['id'].casefold()); names.add(item['filename'].casefold())
                if item['id'] in existing:
                    if existing[item['id']]['definition'] != encode(item):
                        raise ValueError('Existing task changed; append a revision instead')
                    continue
                self.inside('output/' + item['filename'])
                for name in item['inputs']:
                    self.inside(name)
                if self.inside('output/' + item['filename']).exists():
                    raise ValueError('Unregistered output conflict; recover the original ledger')
                normalized.append(item)
            graph = {k: json.loads(v['refs']) for k, v in existing.items()}
            graph.update({i['id']: i['refs'] for i in normalized})
            visited, visiting = set(), set()
            def walk(node):
                if node not in graph:
                    raise ValueError('Unknown reference task: ' + node)
                if node in visiting:
                    raise ValueError('Cyclic task dependencies')
                if node in visited:
                    return
                visiting.add(node)
                for ref in graph[node]:
                    walk(ref)
                visiting.remove(node); visited.add(node)
            for node in graph:
                walk(node)
            for item in normalized:
                self.db.execute('INSERT INTO tasks(id,filename,mode,prompt,refs,inputs,definition,family,version,state) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (item['id'], item['filename'], item['mode'], item['prompt'], encode(item['refs']), encode(item['inputs']),
                     encode(item), item['id'], 1, 'waiting_external' if item['mode'] == 'external' else 'pending'))
            return {'action': 'imported', 'added': len(normalized), 'new_calls': 0}

    def batch(self, batch_id, count, items=None, authorized=False):
        identifier(batch_id)
        if type(count) is not int or count < 1 or not authorized:
            raise ValueError('Explicit authorization and a positive per-batch call limit are required')
        with self.transaction():
            old = self.db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if old:
                if old['limit_calls'] != count or (items is not None and encode(items) != old['items']):
                    raise ValueError('Existing batch ID has different requirements')
                return {'action': 'existing_batch', 'id': batch_id, 'used': self.used(batch_id), 'new_calls': 0}
            if self.active():
                raise ValueError('Recover original dispatched/uncertain job before a new batch')
            selected = items if items is not None else [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE state='pending' ORDER BY rowid")]
            if not selected or len(set(selected)) != len(selected):
                raise ValueError('No distinct pending items in this batch')
            for task_id in selected:
                if self.task(task_id)['state'] != 'pending':
                    raise ValueError('Batch items must be pending')
            self.db.execute('INSERT INTO batches VALUES(?,?,?)', (batch_id, count, encode(selected)))
            self.db.executemany('INSERT INTO batch_items VALUES(?,?,?)', [(batch_id, task_id, i) for i, task_id in enumerate(selected)])
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('current_batch',?)", (batch_id,))
            return {'action': 'batch_ready', 'id': batch_id, 'limit': count, 'items': selected}

    def used(self, batch_id):
        return self.db.execute('SELECT count(*) FROM jobs WHERE batch=?', (batch_id,)).fetchone()[0]

    def active(self):
        row = self.db.execute("SELECT * FROM jobs WHERE phase IN ('dispatched','uncertain') ORDER BY rowid LIMIT 1").fetchone()
        return dict(row) if row else None

    def next(self, tool_ready=False):
        with self.transaction():
            active = self.active()
            if active:
                task = self.task(active['task'])
                return {'action': 'recover_original', 'id': task['id'], 'token': active['token'],
                        'expected_output': str(self.output(task)), 'new_calls': 0}
            current = self.db.execute("SELECT value FROM meta WHERE key='current_batch'").fetchone()
            if not current:
                raise ValueError('Authorize a batch first')
            batch = self.db.execute('SELECT * FROM batches WHERE id=?', (current[0],)).fetchone()
            used = self.used(batch['id'])
            if used >= batch['limit_calls']:
                return {'action': 'batch_limit', 'used': used, 'limit': batch['limit_calls'], 'new_calls': 0}
            blocked = []
            candidates = self.db.execute("SELECT t.* FROM batch_items b JOIN tasks t ON t.id=b.task WHERE b.batch=? AND t.state IN ('pending','needs_review','failed') ORDER BY b.position", (batch['id'],)).fetchall()
            for candidate in candidates:
                task = dict(candidate)
                task_id = task['id']
                if task['state'] in ('needs_review', 'failed'):
                    return {'action': 'needs_review', 'id': task_id, 'new_calls': 0}
                if task['state'] != 'pending':
                    continue
                refs = [self.task(ref) for ref in json.loads(task['refs'])]
                if any(r['state'] != 'completed' and not (r['id'] == task['parent'] and r['state'] == 'needs_review') for r in refs):
                    blocked.append(task_id); continue
                if not tool_ready:
                    raise ValueError('Native image tool must be available before dispatch')
                inputs = [{'path': str(self.verify(r)), 'sha256': r['sha256']} for r in refs]
                for value in json.loads(task['inputs']):
                    path = self.inside(value)
                    inputs.append({'path': str(path), 'sha256': sha(path)})
                if task['mode'] == 'edit' and not inputs:
                    raise ValueError('Edit requires a real source image')
                records, seen = [], set()
                for record in inputs:
                    digest = record['sha256']
                    if digest not in seen:
                        records.append(record); seen.add(digest)
                if len(records) > 5:
                    raise ValueError('Required references exceed current native tool capacity')
                if self.output(task).exists():
                    raise ValueError('Output exists; recover original result without another call')
                token = uuid.uuid4().hex
                self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL)', (token, task_id, batch['id'], 'dispatched', encode(records)))
                self.db.execute("UPDATE tasks SET state='dispatched' WHERE id=?", (task_id,))
                return {'action': 'call_image_tool', 'id': task_id, 'token': token, 'mode': task['mode'],
                        'prompt': task['prompt'], 'references': records, 'filename': task['filename'],
                        'used': used + 1, 'limit': batch['limit_calls'], 'new_calls': 1}
            return {'action': 'blocked' if blocked else 'done', 'blocked': blocked, 'new_calls': 0}

    def complete(self, task_id, source, token=None, external=False, note=''):
        with self.transaction():
            task = self.task(task_id)
            if task['state'] in ('completed', 'needs_review'):
                path = self.verify(task)
                if sha(source) != task['sha256']:
                    raise ValueError('Repeated completion provided different pixels')
                return {'action': 'already_saved', 'file': str(path), 'new_calls': 0}
            if external:
                if task['mode'] != 'external' or task['state'] != 'waiting_external' or not note.strip():
                    raise ValueError('External registration requires the real frame source description')
            else:
                self.job(task_id, token)
            target = self.output(task)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.result-', suffix='.png', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as dest, Path(source).open('rb') as src:
                    for chunk in iter(lambda: src.read(1024 * 1024), b''):
                        dest.write(chunk)
                    dest.flush(); os.fsync(dest.fileno())
                with Image.open(temp) as image:
                    image.verify()
                with Image.open(temp) as image:
                    image.load()
                    if image.format != 'PNG':
                        raise ValueError('Save the actual native PNG result, no disguised conversion')
                    width, height = image.size
                digest = sha(temp)
                if target.exists():
                    if sha(target) != digest:
                        raise ValueError('Output conflict; existing pixels preserved')
                else:
                    # Atomic exclusive publish. A crash before DB commit leaves a recoverable whole image.
                    if os.name == 'nt':
                        # Windows rename is atomic and fails when target already exists.
                        os.rename(temp, target)
                    else:
                        os.link(temp, target)
                self.db.execute("UPDATE tasks SET state='completed',sha256=?,width=?,height=?,note=? WHERE id=?",
                                (digest, width, height, note, task_id))
                if not external:
                    self.db.execute("UPDATE jobs SET phase='completed' WHERE token=?", (token,))
                return {'action': 'saved', 'id': task_id, 'file': str(target), 'sha256': digest,
                        'width': width, 'height': height, 'new_calls': 0}
            finally:
                Path(temp).unlink(missing_ok=True)

    def job(self, task_id, token):
        row = self.db.execute("SELECT * FROM jobs WHERE token=? AND task=? AND phase IN ('dispatched','uncertain')", (token, task_id)).fetchone()
        if not row:
            raise ValueError('Use the original active task token')
        return dict(row)

    def mark(self, task_id, token, note, resolve=False):
        if not note.strip():
            raise ValueError('A specific reason/evidence is required')
        with self.transaction():
            self.job(task_id, token)
            phase = 'failed' if resolve else 'uncertain'
            self.db.execute('UPDATE jobs SET phase=?,note=? WHERE token=?', (phase, note, token))
            self.db.execute('UPDATE tasks SET state=?,note=? WHERE id=?', (phase, note, task_id))
            return {'action': 'resolved_not_submitted' if resolve else 'recover_original', 'id': task_id, 'new_calls': 0}

    def revise(self, source_id, request_id, mode, prompt=None, without_reference=False):
        identifier(request_id)
        if mode not in ('edit', 'regenerate'):
            raise ValueError('Revision mode must be edit or regenerate')
        definition = encode([source_id, mode, prompt, without_reference])
        with self.transaction():
            previous = self.db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['definition'] != definition:
                    raise ValueError('Revision request ID already used for another requirement')
                return json.loads(previous['result'])
            source = self.task(source_id)
            if source['state'] not in ('completed', 'needs_review'):
                raise ValueError('Revision requires an existing saved source version')
            self.verify(source)
            if mode == 'edit' and (not prompt or not prompt.strip() or without_reference):
                raise ValueError('Editing needs a specific change and the source image')
            version = self.db.execute('SELECT max(version)+1 FROM tasks WHERE family=?', (source['family'],)).fetchone()[0]
            task_id = source['family'] + '-v' + str(version)
            identifier(task_id)
            base = self.task(source['family'])
            name = PurePosixPath(base['filename'])
            filename = str(name.with_name(name.stem + '_v' + str(version) + '.png'))
            full = source['prompt']
            if mode == 'edit':
                full += '\nUser requested change: ' + prompt.strip() + '\nEdit only this change; keep other content.'
            elif prompt:
                full += '\nNew user requirements: ' + prompt.strip()
            if mode == 'regenerate':
                full += ('\nCURRENT OPERATION: generate one fresh alternative under the complete content requirements; '
                         'no extra variants. Earlier instructions to freeze pose/composition applied only to earlier edits; '
                         'the current alternative may use a new composition. Preserve latest requested subject, style and colors.')
            refs = [] if without_reference else [source_id]
            item = {'id': task_id, 'filename': filename, 'mode': 'edit' if mode == 'edit' else 'generate',
                    'prompt': full, 'refs': refs, 'inputs': []}
            if self.inside('output/' + filename).exists():
                raise ValueError('Revision output conflict')
            self.db.execute('INSERT INTO tasks(id,filename,mode,prompt,refs,inputs,definition,parent,family,version,state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, filename, item['mode'], full, encode(refs), '[]', encode(item), source_id, source['family'], version, 'pending'))
            result = {'action': 'revision_ready', 'id': task_id, 'filename': filename, 'parent': source_id, 'version': version, 'new_calls': 0}
            self.db.execute('INSERT INTO requests VALUES(?,?,?)', (request_id, definition, encode(result)))
            return result

    def retry(self, task_id, request_id):
        identifier(request_id)
        definition = encode(['retry_failed', task_id])
        with self.transaction():
            previous = self.db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['definition'] != definition:
                    raise ValueError('Request ID already used for different requirements')
                return json.loads(previous['result'])
            task = self.task(task_id)
            if task['state'] != 'failed' or self.output(task).exists():
                raise ValueError('Retry only confirmed failed/not-submitted tasks without saved output; unknown results must recover first')
            self.db.execute("UPDATE tasks SET state='pending' WHERE id=?", (task_id,))
            result = {'action': 'retry_ready', 'id': task_id, 'new_calls': 0}
            self.db.execute('INSERT INTO requests VALUES(?,?,?)', (request_id, definition, encode(result)))
            return result

    def review(self, task_id, passed, note):
        if not note.strip():
            raise ValueError('Specific inspection notes required')
        with self.transaction():
            task = self.task(task_id)
            if task['state'] not in ('completed', 'needs_review'):
                raise ValueError('Only saved images can be reviewed')
            self.verify(task)
            self.db.execute('UPDATE tasks SET state=?,note=? WHERE id=?', ('completed' if passed else 'needs_review', note, task_id))
            return {'action': 'reviewed', 'id': task_id, 'passed': passed, 'new_calls': 0}

    def status(self, verify=False, aspect_ratio=None):
        tasks = [dict(r) for r in self.db.execute('SELECT id,filename,state,parent,family,version,sha256,width,height,note FROM tasks ORDER BY rowid')]
        if aspect_ratio:
            if not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', aspect_ratio):
                raise ValueError('Expected aspect ratio must be positive W:H')
            a, b = map(int, aspect_ratio.split(':'))
            for task in tasks:
                if task['width'] and task['height']:
                    # Allow one pixel of rounding; report only, never redraw/resize.
                    task['aspect_ratio_match'] = abs(task['width'] * b - task['height'] * a) <= max(a, b)
                    task['expected_aspect_ratio'] = aspect_ratio
        if verify:
            for task in tasks:
                if task['state'] in ('completed', 'needs_review'):
                    self.verify(task)
        batches = [dict(r, used=self.used(r['id'])) for r in self.db.execute('SELECT id,limit_calls FROM batches ORDER BY rowid')]
        active = self.active()
        current = self.db.execute("SELECT value FROM meta WHERE key='current_batch'").fetchone()
        return {'action': 'status', 'tasks': tasks, 'batches': batches,
                'current_batch': current[0] if current else None,
                'active': {k: active[k] for k in ('task', 'token', 'phase', 'batch')} if active else None, 'new_calls': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    p = sub.add_parser('import'); p.add_argument('--plan', required=True)
    p = sub.add_parser('batch'); p.add_argument('--id', required=True); p.add_argument('--count', type=int, required=True); p.add_argument('--items', nargs='+'); p.add_argument('--authorized', action='store_true')
    p = sub.add_parser('next'); p.add_argument('--tool-ready', action='store_true')
    for action in ('complete', 'external'):
        p = sub.add_parser(action); p.add_argument('--id', required=True); p.add_argument('--image', required=True); p.add_argument('--note', default='')
        if action == 'complete': p.add_argument('--token', required=True)
    for action in ('uncertain', 'resolve'):
        p = sub.add_parser(action); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--note', required=True)
        if action == 'resolve': p.add_argument('--confirm-not-submitted', action='store_true', required=True)
    p = sub.add_parser('revise'); p.add_argument('--source', required=True); p.add_argument('--request-id', required=True); p.add_argument('--mode', choices=['edit', 'regenerate'], required=True); p.add_argument('--prompt'); p.add_argument('--without-reference', action='store_true')
    p = sub.add_parser('retry'); p.add_argument('--id', required=True); p.add_argument('--request-id', required=True)
    p = sub.add_parser('review'); p.add_argument('--id', required=True); p.add_argument('--result', choices=['pass', 'fail'], required=True); p.add_argument('--note', required=True)
    p = sub.add_parser('status'); p.add_argument('--verify', action='store_true'); p.add_argument('--aspect-ratio')
    args = parser.parse_args(argv)
    project = None
    try:
        project = Project.init(args.project) if args.action == 'init' else Project(args.project)
        if args.action == 'init': result = {'action': 'ready', 'project': str(project.root)}
        elif args.action == 'import': result = project.import_plan(json.loads(Path(args.plan).read_text(encoding='utf-8-sig')))
        elif args.action == 'batch': result = project.batch(args.id, args.count, args.items, args.authorized)
        elif args.action == 'next': result = project.next(args.tool_ready)
        elif args.action in ('complete', 'external'): result = project.complete(args.id, args.image, getattr(args, 'token', None), args.action == 'external', args.note)
        elif args.action in ('uncertain', 'resolve'): result = project.mark(args.id, args.token, args.note, args.action == 'resolve')
        elif args.action == 'revise': result = project.revise(args.source, args.request_id, args.mode, args.prompt, args.without_reference)
        elif args.action == 'retry': result = project.retry(args.id, args.request_id)
        elif args.action == 'review': result = project.review(args.id, args.result == 'pass', args.note)
        else: result = project.status(args.verify, args.aspect_ratio)
        print(encode(result)); return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        print(encode({'action': 'error', 'error': str(error), 'new_calls': 0}), file=sys.stderr); return 2
    finally:
        if project: project.close()


if __name__ == '__main__':
    sys.exit(main())
