"""Run the real event loop and hook subprocesses, without changing networking."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'usr/lib/iserv/ipv6chdetectd'

class DaemonTest(unittest.TestCase):
    def test_each_hook_run_collects_exactly_one_followup(self):
        self.assertTrue((LIB / 'daemon.py').exists(), 'foreground event loop is missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / 'hooks'
            hooks.mkdir()
            log = root / 'log'
            release = root / 'release'
            started = root / 'started'
            hook = hooks / '10test'
            hook.write_text(f'''#!/bin/sh
[ "$IPV6CHDETECTD_PHASE" = prepare ] || exit 0
echo run >> '{log}'
touch '{started}'
count=$(wc -l < '{log}')
while [ ! -e "{release}-$count" ]; do sleep 0.02; done
''')
            hook.chmod(0o755)
            code = f"import sys; sys.path.insert(0,{str(LIB)!r}); from daemon import serve; serve({str(root/'state')!r}, {str(hooks)!r}, debounce=0.1, monitor=False)"
            proc = subprocess.Popen([sys.executable, '-c', code])
            def wait(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    if proc.poll() is not None:
                        self.fail('worker exited prematurely')
                    time.sleep(0.02)
                self.fail('event worker did not make progress')
            try:
                wait(started.exists)
                sys.path.insert(0, str(LIB))
                from daemon import notify
                for _ in range(15):
                    notify(root / 'state')
                time.sleep(0.15)
                self.assertEqual(len(log.read_text().splitlines()), 1)
                Path(str(release)+'-1').touch()
                wait(lambda: len(log.read_text().splitlines()) == 2)
                for _ in range(10):
                    notify(root / 'state')
                time.sleep(0.15)
                self.assertEqual(len(log.read_text().splitlines()), 2)
                Path(str(release)+'-2').touch()
                wait(lambda: len(log.read_text().splitlines()) == 3)
                Path(str(release)+'-3').touch()
                time.sleep(0.3)
                self.assertEqual(log.read_text().splitlines(), ['run', 'run', 'run'])
            finally:
                proc.terminate()
                proc.wait(timeout=10)

if __name__ == '__main__':
    unittest.main()

class AdapterTest(unittest.TestCase):
    def test_sourced_client_hooks_filter_ipv4_and_return_to_caller(self):
        paths = [
            ROOT / 'etc/dhcp/dhclient-exit-hooks.d/ipv6chdetectd',
            ROOT / 'usr/lib/dhcpcd/dhcpcd-hooks/95-ipv6chdetectd',
        ]
        for source in paths:
            self.assertTrue(source.exists(), f'{source} source adapter is missing')
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                log = root / 'log'
                command = root / 'notify'
                command.write_text(f'#!/bin/sh\necho event >> "{log}"\n')
                command.chmod(0o755)
                hook = root / 'hook'
                hook.write_text(source.read_text().replace('/usr/sbin/ipv6chdetectd', str(command)))
                for reason, expected in [('BOUND', 0), ('BOUND6', 1), ('EXPIRE6', 2)]:
                    result = subprocess.run(['/bin/sh', '-c', f'. "{hook}"; echo returned'],
                                            env=dict(os.environ, reason=reason), capture_output=True, text=True)
                    self.assertEqual(result.stdout.strip(), 'returned')
                    self.assertEqual(len(log.read_text().splitlines()) if log.exists() else 0, expected)

class NotificationTest(unittest.TestCase):
    def test_readonly_client_can_notify_running_worker(self):
        import errno
        import socket
        from unittest.mock import patch
        sys.path.insert(0, str(LIB))
        import daemon
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(str(root / 'notify.sock'))
                receiver.settimeout(1)
                with patch.object(daemon.EventQueue, 'notify', side_effect=OSError(errno.EROFS, 'read-only client namespace')):
                    daemon.notify(root)
                self.assertEqual(receiver.recv(100), b'change')
