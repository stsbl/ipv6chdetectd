import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'usr/lib/iserv/ipv6chdetectd/hooks.py'

class HookTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'shared hook runner is missing')
        spec = importlib.util.spec_from_file_location('hooks', MODULE)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'config'
        self.target.write_text('old')
        self.calls = []
        self.fail_apply = self.fail_gen = False
        def run(args, **kwargs):
            self.calls.append(args)
            if args[:2] == ['iconf', 'gen']:
                return subprocess.CompletedProcess(args, int(self.fail_gen), b'new')
            if args[:2] == ['iconf', 'repair']:
                self.target.write_text('new')
            if args[:2] == ['systemctl', 'reload'] and self.fail_apply:
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)
        self.runner = self.mod.HookRunner(self.root / 'state', run=run, schema_root=None)
        self.records = [
            ['config', 'test.service', 'reload', str(self.target)],
            ['config', 'test.service', 'reload', str(self.target)],
            ['validate', 'test.service', 'check-config', str(self.target)],
        ]

    def test_shared_target_once_and_unchanged_no_reload(self):
        self.assertTrue(self.runner.apply(self.records))
        self.assertEqual(sum(x[:2] == ['iconf', 'repair'] for x in self.calls), 1)
        self.assertEqual(sum(x[:2] == ['systemctl', 'reload'] for x in self.calls), 1)
        self.calls.clear()
        self.assertTrue(self.runner.apply(self.records))
        self.assertFalse(any(x[:2] == ['systemctl', 'reload'] for x in self.calls))

    def test_failed_apply_retried_when_file_already_written(self):
        self.fail_apply = True
        self.assertFalse(self.runner.apply(self.records))
        self.assertEqual(self.target.read_text(), 'new')
        self.fail_apply = False
        self.calls.clear()
        self.runner = self.mod.HookRunner(self.root / 'state', run=self.runner.run, schema_root=None)
        self.assertTrue(self.runner.apply(self.records))
        self.assertIn(['systemctl', 'reload', 'test.service'], self.calls)

    def test_generation_failure_blocks_service(self):
        self.fail_gen = True
        self.assertFalse(self.runner.apply(self.records))
        self.assertFalse(any(x[:2] == ['systemctl', 'reload'] for x in self.calls))
        self.assertEqual(self.target.read_text(), 'old')

    def test_watch_detects_cached_config_change_without_generated_file(self):
        records = [['watch', 'cached.service', 'restart', str(self.target)]]
        self.assertTrue(self.runner.apply(records))
        self.calls.clear()
        self.target.write_text('different LAN')
        self.assertTrue(self.runner.apply(records))
        self.assertIn(['systemctl', 'restart', 'cached.service'], self.calls)

    def test_validation_failure_is_retryable_without_rewriting(self):
        original = self.runner.run
        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == 'check-config':
                result.returncode = 1
            return result
        self.runner.run = run
        self.assertFalse(self.runner.apply(self.records))
        self.assertNotIn(['systemctl', 'reload', 'test.service'], self.calls)
        self.runner.run = original
        self.assertTrue(self.runner.apply(self.records))
        self.assertIn(['systemctl', 'reload', 'test.service'], self.calls)

    def test_stopped_service_stays_stopped(self):
        original = self.runner.run
        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['systemctl', 'is-active']:
                result.returncode = 3
            return result
        self.runner.run = run
        self.assertTrue(self.runner.apply(self.records))
        self.assertFalse(any(x[:2] in (['systemctl', 'reload'], ['systemctl', 'start'], ['systemctl', 'restart']) for x in self.calls))

    def test_one_failed_consumer_does_not_suppress_another(self):
        self.fail_apply = True
        records = self.records + [['watch', 'other.service', 'restart', str(self.target)]]
        self.assertFalse(self.runner.apply(records))
        self.assertIn(['systemctl', 'restart', 'other.service'], self.calls)

    def test_file_changed_during_action_is_applied_next_pass(self):
        records = [['watch', 'test.service', 'reload', str(self.target)]]
        original = self.runner.run
        first = True
        def run(argv, **kwargs):
            nonlocal first
            result = original(argv, **kwargs)
            if argv[:2] == ['systemctl', 'reload'] and first:
                first = False
                self.target.write_text('event received during reload')
            return result
        self.runner.run = run
        self.assertTrue(self.runner.apply(records))
        self.calls.clear()
        self.assertTrue(self.runner.apply(records))
        self.assertIn(['systemctl', 'reload', 'test.service'], self.calls)

    def test_failed_preparation_does_not_suppress_independent_hook(self):
        import json
        hooks = self.root / 'hooks'
        hooks.mkdir()
        state = self.root / 'collector-state'
        state.mkdir()
        output = self.root / 'applied'
        good = hooks / '10good'
        records = [['watch', '@good', 'restart', str(self.target)], ['action', '@good', 'touch', str(output)]]
        good.write_text('#!/bin/sh\n[ "$IPV6CHDETECTD_PHASE" = configure ] || exit 0\ncat > "$IPV6CHDETECTD_REQUESTS" <<\'EOF\'\n'+''.join(json.dumps(r)+'\n' for r in records)+'EOF\n')
        good.chmod(0o755)
        bad = hooks / '20bad'
        bad.write_text('#!/bin/sh\n[ "$IPV6CHDETECTD_PHASE" != prepare ] || exit 1\n')
        bad.chmod(0o755)
        self.assertFalse(self.mod.collect_and_apply(hooks, state))
        self.assertTrue(output.exists(), 'an unrelated failed preparation suppressed application')

    def test_preparation_dependency_blocks_only_its_consumers(self):
        good = self.root / 'good-applied'
        bad = self.root / 'bad-applied'
        records = [
            ['watch', '@good', 'restart', str(self.target)],
            ['action', '@good', 'touch', str(good)],
            ['watch', '@dependent', 'restart', str(self.target)+'-separate'],
            ['action', '@dependent', 'touch', str(bad)],
            ['requires', '@dependent', 'failed-preparation'],
        ]
        runner = self.mod.HookRunner(self.root / 'real-dependency-state', schema_root=None)
        self.assertFalse(runner.apply(records, {'failed-preparation'}))
        self.assertTrue(good.exists())
        self.assertFalse(bad.exists())

    def test_timeout_kills_hook_descendants_before_retry(self):
        import time
        output = self.root / 'orphan-output'
        runner = self.mod.HookRunner(self.root / 'real-timeout-state')
        runner.timeout = 0.05
        result = runner.command(['/bin/sh', '-c', f'(sleep 0.2; touch "{output}") & wait'])
        self.assertNotEqual(result.returncode, 0, 'hook did not time out')
        time.sleep(0.3)
        self.assertFalse(output.exists(), 'timed-out helper survived and wrote after the pass ended')

    def test_required_preparation_cannot_prevent_address_loss_shutdown(self):
        records = [['state', 'test.service', 'stop'], ['requires', 'test.service', 'failed-hosts']]
        self.assertTrue(self.runner.apply(records, {'failed-hosts'}))
        self.assertIn(['systemctl', 'stop', 'test.service'], self.calls)
