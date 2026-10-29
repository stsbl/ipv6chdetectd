"""Exercise package-owned scripts with isolated installed-package fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent

class ConsumersTest(unittest.TestCase):
    def collect(self, package, installed, bind_files=None):
        if not (WORKSPACE / package).is_dir():
            self.skipTest('consumer integration requires sibling source checkout: ' + package)
        hook = WORKSPACE / package / 'lib/ipv6chdetectd.d' / {'stsbl-iserv-config-ipv6': '40config-ipv6', 'stsbl-iserv-ipv6-support': '50ipv6-support', 'stsbl-iserv-dns-over-tls': '60dns-over-tls'}[package]
        self.assertTrue(hook.exists(), 'package configuration hook is missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = REPO / 'usr/lib/iserv/ipv6chdetectd/request'
            functions = root / 'functions'
            functions.write_text((REPO / 'usr/lib/iserv/ipv6chdetectd-functions').read_text().replace('/usr/lib/iserv/ipv6chdetectd/request', str(request)))
            cfg = root / 'cfg'
            cfg.write_text('UpdateIPv6LAN=\nUpdateIPv6Regdns=\nDHCP="*"\n')
            script = root / 'script'
            source = hook.read_text().replace('/usr/lib/iserv/ipv6chdetectd-functions', str(functions)).replace('/usr/lib/iserv/cfg', str(cfg))
            if bind_files is not None:
                bind = root / 'bind'
                bind.mkdir()
                for name in bind_files:
                    (bind / name).write_text('historical or active zone fixture')
                source = source.replace('/etc/bind', str(bind))
                query = root / 'netquery6'
                query.write_text('#!/bin/sh\necho enp4s0\n')
                query.chmod(0o755)
            script.write_text(source)
            command = root / 'iservpkginstalled'
            command.write_text('#!/bin/sh\ncase "$1" in\n' + '|'.join(installed or ['NONE']) + ') exit 0;;\n*) exit 1;; esac\n')
            command.chmod(0o755)
            output = root / 'requests'
            output.touch()
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], IPV6CHDETECTD_PHASE='configure', IPV6CHDETECTD_REQUESTS=str(output))
            subprocess.run(['/bin/bash', str(script)], env=env, check=True)
            return [json.loads(line) for line in output.read_text().splitlines()]

    def test_absent_optional_packages_register_nothing(self):
        self.assertEqual(self.collect('stsbl-iserv-config-ipv6', []), [])
        self.assertEqual(self.collect('stsbl-iserv-ipv6-support', []), [])

    def test_base_only_includes_ssh_exim_bind_without_portal(self):
        records = self.collect('stsbl-iserv-config-ipv6', ['iserv-update','iserv-server-exim','iserv-server-bind'])
        configs = [r for r in records if r[0] == 'config']
        self.assertTrue(any('/etc/ssh/sshd_config' in r for r in configs))
        self.assertTrue(any('/etc/exim4/exim4.conf' in r for r in configs))
        self.assertTrue(any('/etc/bind/named.conf.iserv' in r for r in configs))
        self.assertFalse(any('nginx' in str(r) for r in records))

    def test_portal_registers_nginx_and_radius_without_apache(self):
        records = self.collect('stsbl-iserv-ipv6-support', ['iserv-portal','iserv-server-nginx','iserv-wlan','iserv-server-freeradius'])
        self.assertTrue(any('/etc/nginx/server-iserv' in r for r in records))
        self.assertTrue(any('/etc/freeradius/3.0/clients.conf' in r for r in records))
        self.assertFalse(any('apache' in str(r) or 'homepage' in str(r) for r in records))

    def test_restricted_tls_listener_updates_without_optional_tunnel_package(self):
        records = self.collect('stsbl-iserv-dns-over-tls', ['stunnel4'])
        self.assertTrue(any('/etc/stunnel/iserv-dns-over-tls.conf' in r for r in records))

    def test_bind_backup_files_are_never_registered_as_live_zones(self):
        backups = [
            'db.iserv.20190208', 'db.forward.20190208-1',
            'db.iserv.fallback.20260827-23', 'db.forward.enp4s0.20261029',
            'db.iserv.20190208-1.20261029', 'db.forward.enp4s0.bak',
            'db.iserv.enp4s0.orig', 'db.forward.enp4s0.dpkg-old',
        ]
        active = ['db.iserv.enp4s0', 'db.forward.enp4s0',
                  'db.iserv.removed0', 'db.forward.removed0']
        records = self.collect('stsbl-iserv-ipv6-support',
                               ['iserv-portal', 'iserv-server-bind'],
                               bind_files=backups + active)
        paths = {Path(path).name for record in records if record[0] == 'config'
                 for path in record[3:]}
        self.assertTrue(set(active).issubset(paths))
        self.assertTrue(set(backups).isdisjoint(paths), paths.intersection(backups))
        self.assertLess(len(paths), 15, 'historical zone backups expanded the work batch')

if __name__ == '__main__':
    unittest.main()

class QueryFailureTest(unittest.TestCase):
    def test_address_errors_do_not_enter_address_loss_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = root / 'netquery6'
            functions = REPO / 'usr/lib/iserv/ipv6chdetectd-functions'
            for status, expected in [(1, 'absent'), (2, '')]:
                query.write_text(f'#!/bin/sh\nexit {status}\n')
                query.chmod(0o755)
                code = f'. "{functions}"; if ipv6_has_addresses; then echo present; else echo absent; fi'
                result = subprocess.run(['/bin/sh','-c',code],env=dict(os.environ,PATH=str(root)+':'+os.environ['PATH']),capture_output=True,text=True)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(result.returncode, 0 if status == 1 else 2)
