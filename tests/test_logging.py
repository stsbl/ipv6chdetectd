"""Verify journal output using real child processes, not mocked logging calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'usr/lib/iserv/ipv6chdetectd'

class LoggingTest(unittest.TestCase):
    def run_check(self, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/'config'
            target.write_text('configured')
            validator="import sys; print('CHECK_STDOUT'); print('CHECK_STDERR',file=sys.stderr); sys.exit(%d)" % (2 if fail else 0)
            action="import sys; print('ACTION_STDOUT'); print('ACTION_STDERR',file=sys.stderr)"
            records=[['watch','@test','restart',str(target)],
                     ['validate','@test',sys.executable,'-c',validator],
                     ['action','@test',sys.executable,'-c',action]]
            script=(f"import sys,logging; sys.path.insert(0,{str(LIB)!r}); "
                    "from hooks import HookRunner; logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s'); "
                    f"sys.exit(0 if HookRunner({str(root/'state')!r}).apply({records!r}) else 1)")
            return subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)

    def test_successful_check_and_action_output_is_muted(self):
        result=self.run_check()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('CHECK_STDOUT',result.stdout+result.stderr)
        self.assertNotIn('CHECK_STDERR',result.stdout+result.stderr)
        self.assertNotIn('ACTION_STDOUT',result.stdout+result.stderr)
        self.assertNotIn('ACTION_STDERR',result.stdout+result.stderr)
        self.assertIn('[validate @test]',result.stderr)
        self.assertIn('[apply @test]',result.stderr)
        self.assertIn('[summary]',result.stderr)

    def test_failed_check_retains_diagnostics_with_context(self):
        result=self.run_check(fail=True)
        self.assertEqual(result.returncode,1)
        self.assertIn('ERROR: [validate @test]',result.stderr)
        self.assertIn('CHECK_STDOUT',result.stderr)
        self.assertIn('CHECK_STDERR',result.stderr)
        self.assertNotIn('ACTION_STDOUT',result.stdout+result.stderr)

    def run_command_script(self, body, payload=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            program=root/'check.py'
            program.write_text(body)
            options=", stdout=subprocess.PIPE" if payload else ""
            context='generate test.conf' if payload else 'validate test.conf'
            wrapper=(f"import sys,logging,subprocess; sys.path.insert(0,{str(LIB)!r}); "
                     "from hooks import HookRunner; logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s'); "
                     f"r=HookRunner({str(root/'state')!r}).command([sys.executable,{str(program)!r}],context={context!r}{options}); sys.exit(r.returncode)")
            return subprocess.run([sys.executable,'-c',wrapper],capture_output=True,text=True)

    def test_failed_generator_never_logs_generated_configuration(self):
        result=self.run_command_script("import sys; print('GENERATED_SECRET'); print('template failed',file=sys.stderr); sys.exit(2)",payload=True)
        self.assertEqual(result.returncode,2)
        self.assertNotIn('GENERATED_SECRET',result.stdout+result.stderr)
        self.assertIn('ERROR: [generate test.conf] stderr: template failed',result.stderr)

    def test_explicit_error_is_retained_even_when_checker_returns_success(self):
        result=self.run_command_script("import sys; print('ordinary progress'); print('timestamp| ERROR: duplicate entry ignored',file=sys.stderr)")
        self.assertEqual(result.returncode,0)
        self.assertNotIn('ordinary progress',result.stdout+result.stderr)
        self.assertIn('ERROR: [validate test.conf] stderr: timestamp| ERROR: duplicate entry ignored',result.stderr)

if __name__=='__main__': unittest.main()
