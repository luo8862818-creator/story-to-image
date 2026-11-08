"""Offline persistence/dispatch invariants. No image service calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/project.py'
spec = importlib.util.spec_from_file_location('project', SCRIPT)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
Project = module.Project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        block = patch('socket.socket.connect', side_effect=AssertionError('No network in local manager'))
        block.start(); self.addCleanup(block.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'project with spaces'
        self.p = Project.init(self.path); self.addCleanup(lambda: self.p.close())
        self.image = self.root / 'result.png'; Image.new('RGB', (64, 36), 'blue').save(self.image)
        self.doc = {'tasks': [self.item('A', 'CHAR_HIPPO_MOM.png'), self.item('B', 'SH002-KF01.png', refs=['A'])]}

    def item(self, task_id, filename=None, mode='generate', refs=None):
        return {'id': task_id, 'filename': filename or task_id + '.png', 'mode': mode,
                'prompt': 'Adult anime woman in a white jacket; original complete requirements', 'refs': refs or [], 'inputs': []}

    def begin(self, count=1, items=None, batch='batch-1'):
        self.p.import_plan(self.doc)
        self.p.batch(batch, count, items, True)
        return self.p.next(True)

    def finish(self, receipt):
        return self.p.complete(receipt['id'], self.image, receipt['token'])

    def reopen(self):
        self.p.close(); self.p = Project(self.path)

    def test_limit_replay_cold_reopen_and_second_batch(self):
        first = self.begin()
        self.assertEqual(self.p.next(True)['action'], 'recover_original')
        self.assertNotIn('prompt', self.p.next(True))
        self.finish(first); self.reopen()
        self.assertEqual(self.p.next(True)['action'], 'batch_limit')
        self.assertEqual(self.p.batch('batch-1', 1, authorized=True)['action'], 'existing_batch')
        self.assertEqual(self.p.next(True)['new_calls'], 0)
        self.p.batch('batch-2', 1, authorized=True)
        second = self.p.next(True)
        self.assertEqual(second['id'], 'B')
        self.assertTrue(second['references'][0]['path'].endswith('CHAR_HIPPO_MOM.png'))
        self.finish(second)
        self.assertEqual(self.p.status()['batches'][1]['used'], 1)

    def test_separate_cli_process_resumes_same_job(self):
        first = self.begin()
        cli = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--project', str(self.path), 'next', '--tool-ready'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        result = json.loads(cli.stdout)
        self.assertEqual(result['token'], first['token']); self.assertEqual(result['new_calls'], 0)

    def test_unknown_job_blocks_new_batch_and_recovers_original(self):
        first = self.begin()
        self.p.mark(first['id'], first['token'], 'Simulated disconnected tool')
        self.reopen()
        with self.assertRaises(ValueError): self.p.batch('new', 1, authorized=True)
        self.assertEqual(self.p.next(True)['token'], first['token'])
        self.finish(first)
        self.assertEqual(self.p.next(True)['action'], 'batch_limit')

    def test_malformed_image_wrong_token_and_output_conflict_keep_active(self):
        first = self.begin()
        with self.assertRaises(ValueError): self.p.complete('A', self.image, 'wrong')
        bad = self.root / 'bad.png'; bad.write_bytes(b'not an image')
        with self.assertRaises(OSError): self.p.complete('A', bad, first['token'])
        output = self.path / 'output/CHAR_HIPPO_MOM.png'; output.parent.mkdir(exist_ok=True)
        Image.new('RGB', (64, 36), 'red').save(output)
        with self.assertRaisesRegex(ValueError, 'conflict'): self.finish(first)
        self.assertEqual(self.p.next(True)['action'], 'recover_original')

    def test_image_commit_before_db_failure_recovers_without_extra_dispatch(self):
        first = self.begin()
        original = self.p.output
        def interrupt_after_publish(task):
            return original(task)
        # Simulate rollback after a complete filesystem publish, before DB commit.
        with patch.object(self.p, 'transaction') as wrapper:
            from contextlib import contextmanager
            @contextmanager
            def broken():
                self.p.db.execute('BEGIN IMMEDIATE')
                try:
                    yield
                    raise OSError('Injected database commit failure')
                finally:
                    self.p.db.execute('ROLLBACK')
            wrapper.side_effect = broken
            with self.assertRaises(OSError): self.finish(first)
        self.reopen()
        self.assertEqual(self.p.next(True)['token'], first['token'])
        self.p.complete('A', self.path / 'output/CHAR_HIPPO_MOM.png', first['token'])
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_revision_replay_historical_source_and_full_requirements(self):
        self.finish(self.begin())
        old = module.sha(self.path / 'output/CHAR_HIPPO_MOM.png')
        revision = self.p.revise('A', 'fix-1', 'edit', 'Change jacket to red')
        self.assertEqual(self.p.revise('A', 'fix-1', 'edit', 'Change jacket to red'), revision)
        self.p.batch('fix-batch', 1, [revision['id']], True)
        receipt = self.p.next(True)
        self.assertIn('original complete requirements', receipt['prompt'])
        self.finish(receipt)
        third = self.p.revise('A', 'fix-old', 'edit', 'Change jacket to green')
        self.assertEqual(third['version'], 3); self.assertEqual(third['parent'], 'A')
        reroll = self.p.revise(revision['id'], 'reroll-1', 'regenerate')
        prompt = self.p.task(reroll['id'])['prompt']
        self.assertIn('original complete requirements', prompt); self.assertIn('Change jacket to red', prompt)
        self.assertEqual(module.sha(self.path / 'output/CHAR_HIPPO_MOM.png'), old)
        self.assertEqual(self.p.task('B')['state'], 'pending')

    def test_bad_review_pauses_and_explicit_revision_can_use_bad_image(self):
        first = self.begin(count=2); self.finish(first)
        self.p.review('A', False, 'Specific issue in outfit')
        self.assertEqual(self.p.next(True)['action'], 'needs_review')
        revision = self.p.revise('A', 'fix', 'edit', 'Fix the outfit')
        self.p.batch('fix-only', 1, [revision['id']], True)
        self.assertEqual(self.p.next(True)['id'], revision['id'])

    def test_external_frame_blocks_without_fabricating_image(self):
        self.doc = {'tasks': [self.item('EXT', 'SH003-START-FROM-SH002-TAIL.png', 'external'), self.item('END', 'SH003-KF02.png', 'edit', ['EXT'])]}
        result = self.begin()
        self.assertEqual(result['action'], 'blocked'); self.assertEqual(self.p.used('batch-1'), 0)
        self.p.complete('EXT', self.image, external=True, note='Synthetic fixture for a real extracted frame')
        self.assertEqual(self.p.next(True)['id'], 'END')

    def test_reference_corruption_is_detected_before_charge(self):
        self.finish(self.begin())
        self.p.batch('next', 1, authorized=True)
        (self.path / 'output/CHAR_HIPPO_MOM.png').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.p.next(True)
        self.assertEqual(self.p.used('next'), 0)

    def test_move_project_keeps_relative_state_and_references(self):
        self.finish(self.begin()); self.p.close()
        moved = self.root / 'moved'; self.path.rename(moved); self.path = moved; self.p = Project(moved)
        self.p.batch('next', 1, authorized=True)
        self.assertTrue(self.p.next(True)['references'][0]['path'].startswith(str(moved)))

    def test_import_idempotence_collision_cycles_and_path_guards(self):
        self.assertEqual(self.p.import_plan(self.doc)['added'], 2)
        self.assertEqual(self.p.import_plan(self.doc)['added'], 0)
        cases = [ {'tasks':[self.item('X', '../escape.png')]},
                  {'tasks':[self.item('X', 'char_hippo_mom.png')]},
                  {'tasks':[self.item('X', 'CON.png')]},
                  {'tasks':[self.item('X', refs=['Y']),self.item('Y', refs=['X'])]},
                  {'tasks':[self.item('X', refs=['a'])]} ]
        for doc in cases:
            with self.assertRaises((ValueError, sqlite3.Error)): self.p.import_plan(doc)
        self.assertEqual(len(self.p.status()['tasks']), 2)

    def test_tool_missing_and_unauthorized_batch_cost_nothing(self):
        self.p.import_plan(self.doc)
        with self.assertRaises(ValueError): self.p.batch('unauthorized', 1)
        self.p.batch('ok', 1, authorized=True)
        with self.assertRaises(ValueError): self.p.next(False)
        self.assertEqual(self.p.used('ok'), 0)

    def test_duplicate_batch_cannot_increase_limit_or_change_scope(self):
        self.begin()
        with self.assertRaises(ValueError): self.p.batch('batch-1', 2, authorized=True)
        with self.assertRaises(ValueError): self.p.batch('batch-1', 1, ['B'], True)
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_two_clients_reserve_only_one_dispatch(self):
        first = self.begin(count=2)
        other = Project(self.path)
        try:
            self.assertEqual(other.next(True)['token'], first['token'])
        finally: other.close()
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_explicit_retry_after_not_submitted_never_refunds_or_replays(self):
        first = self.begin()
        with self.assertRaises(ValueError): self.p.retry('A', 'retry-1')
        self.p.mark('A', first['token'], 'Confirmed no tool accepted this synthetic job', resolve=True)
        self.p.retry('A', 'retry-1')
        self.p.batch('retry-batch', 1, ['A'], True)
        receipt = self.p.next(True)
        self.assertNotEqual(receipt['token'], first['token'])
        self.p.retry('A', 'retry-1') # replay must not reset a dispatched task
        self.assertEqual(self.p.task('A')['state'], 'dispatched')
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_saved_result_ignores_mutated_external_source(self):
        first = self.begin()
        result = self.finish(first)
        Image.new('RGB', (64, 36), 'red').save(self.image)
        with self.assertRaises(ValueError): self.finish(first)
        self.assertEqual(module.sha(result['file']), self.p.task('A')['sha256'])

    def test_simultaneous_clients_return_only_one_new_call(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.p.import_plan(self.doc); self.p.batch('parallel', 2, authorized=True)
        barrier = threading.Barrier(2)
        def worker():
            client = Project(self.path)
            try:
                barrier.wait(timeout=5)
                return client.next(True)
            finally: client.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(r['new_calls'] for r in results), 1)
        self.assertEqual({r['token'] for r in results}, {self.p.active()['token']})

    def test_appended_tasks_do_not_enter_existing_batch(self):
        self.doc['tasks'] = self.doc['tasks'][:1]
        self.finish(self.begin(count=2))
        self.p.import_plan({'tasks': [self.item('NEW')]})
        self.assertEqual(self.p.next(True)['action'], 'done')
        self.assertEqual(self.p.task('NEW')['state'], 'pending')

    def test_normal_dispatch_only_checks_required_history(self):
        self.finish(self.begin())
        self.p.import_plan({'tasks': [self.item('UNRELATED')]})
        (self.path / 'output/CHAR_HIPPO_MOM.png').write_bytes(b'changed')
        self.p.batch('unrelated', 1, ['UNRELATED'], True)
        self.assertEqual(self.p.next(True)['id'], 'UNRELATED')
        with self.assertRaises(ValueError): self.p.status(verify=True)

    def test_aspect_check_reports_real_mismatch_without_new_generation(self):
        self.finish(self.begin())
        self.assertTrue(self.p.status(aspect_ratio='16:9')['tasks'][0]['aspect_ratio_match'])
        report = self.p.status(aspect_ratio='9:16')
        self.assertFalse(report['tasks'][0]['aspect_ratio_match'])
        self.assertEqual(report['tasks'][0]['width'], 64)
        self.assertEqual(self.p.used('batch-1'), 1)
        with self.assertRaises(ValueError): self.p.status(aspect_ratio='0:16')

    def test_pause_reopen_resume_does_not_reset_limit(self):
        self.p.import_plan(self.doc); self.p.batch('pause-test', 2, authorized=True)
        self.p.pause(True, 'User stopped before drawing')
        self.reopen()
        self.assertEqual(self.p.next(True)['action'], 'paused')
        self.assertEqual(self.p.used('pause-test'), 0)
        self.p.pause(False)
        first = self.p.next(True)
        self.p.pause(True, 'User stopped during accepted drawing')
        self.finish(first) # Late result may still be saved while paused.
        self.assertEqual(self.p.next(True)['action'], 'paused')
        self.p.pause(False)
        self.finish(self.p.next(True))
        self.assertEqual(self.p.next(True)['action'], 'batch_limit')
        self.assertTrue(all(t['dispatch_to_save_seconds'] is not None for t in self.p.status()['timings']))

    def test_pause_resume_unknown_job_never_resubmits(self):
        first = self.begin()
        self.p.mark('A', first['token'], 'Unknown native result')
        self.p.pause(True); self.reopen(); self.p.pause(False)
        recovered = self.p.next(True)
        self.assertEqual(recovered['action'], 'recover_original')
        self.assertEqual(recovered['token'], first['token'])
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_later_review_issue_blocks_earlier_newly_ready_item(self):
        self.doc = {'tasks': [self.item('EXT', mode='external'), self.item('A', refs=['EXT']), self.item('B')]}
        first = self.begin(count=3)
        self.assertEqual(first['id'], 'B')
        self.finish(first); self.p.review('B', False, 'Needs user review')
        self.p.complete('EXT', self.image, external=True, note='Synthetic extracted frame')
        result = self.p.next(True)
        self.assertEqual(result['action'], 'needs_review')
        self.assertEqual(self.p.used('batch-1'), 1)

    def test_legacy_timing_migration_preserves_existing_ledger(self):
        first = self.begin()
        old_path = self.root/'legacy'; old_path.mkdir()
        db = sqlite3.connect(old_path/'state.sqlite3')
        old_schema = module.SCHEMA.replace(',\n dispatched_at REAL,completed_at REAL', '')
        db.executescript(old_schema)
        for table in ('tasks','batches','batch_items','jobs','requests'):
            columns = [r[1] for r in db.execute('PRAGMA table_info('+table+')')]
            rows = self.p.db.execute('SELECT '+','.join(columns)+' FROM '+table).fetchall()
            db.executemany('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', rows)
        db.commit(); db.close()
        legacy = Project(old_path)
        try:
            self.assertEqual(legacy.next(True)['token'], first['token'])
            self.assertEqual(legacy.used('batch-1'), 1)
            self.assertIsNone(legacy.status()['timings'][0]['dispatch_to_save_seconds'])
        finally: legacy.close()

    def test_long_story_dependency_chain_does_not_use_recursion(self):
        tasks = [self.item('TASK'+str(i), refs=['TASK'+str(i+1)] if i<1199 else []) for i in range(1200)]
        self.assertEqual(self.p.import_plan({'tasks':tasks})['added'],1200)

    def test_cli_json_is_utf8_even_with_ascii_terminal_encoding(self):
        receipt = self.begin()
        self.p.complete(receipt['id'],self.image,receipt['token'],note='中文文件与状态')
        env=dict(os.environ,PYTHONIOENCODING='ascii')
        result=subprocess.run([sys.executable,str(SCRIPT),'--project',str(self.path),'status'],capture_output=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['tasks'][0]['note'],'中文文件与状态')


if __name__ == '__main__': unittest.main()
