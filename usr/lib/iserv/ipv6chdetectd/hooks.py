"""Collect package declarations and apply shared configuration once per service."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

LOG = logging.getLogger(__name__)
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def run_command(argv, check=False, timeout=300, **kwargs):
    """Bound the lifetime of the entire hook/helper tree, including on shutdown."""
    with subprocess.Popen(argv, start_new_session=True, **kwargs) as process:
        def kill_group():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        def interrupted(signum, _frame):
            kill_group()
            raise SystemExit(128 + signum)
        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group()
                process.communicate()
                raise
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        finally:
            if process.poll() is None:
                kill_group()
                process.wait()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


class HookRunner:
    def __init__(self, directory, run=None, schema_root='/usr/share/iserv/iconf',
                 schema_maps='/usr/share/iserv/update/iconf-schema.d'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.filename = self.directory / 'application.json'
        self.run = run or run_command
        self.timeout = 300
        self.schema_root = Path(schema_root) if schema_root is not None else None
        self.schema_maps = Path(schema_maps)
        try:
            self.state = json.loads(self.filename.read_text())
        except FileNotFoundError:
            self.state = {'pending': {}, 'hashes': {}}

    def save(self):
        atomic_json(self.filename, self.state)

    def command(self, argv, **kwargs):
        try:
            result = self.run(argv, check=False, timeout=self.timeout, **kwargs)
            if result.returncode and argv[:2] not in (['systemctl', 'is-active'], ['systemctl', 'is-enabled']):
                LOG.error('%s exited with status %s', argv, result.returncode)
            return result
        except (OSError, subprocess.TimeoutExpired) as error:
            LOG.error('%s: %s', argv, error)
            return subprocess.CompletedProcess(argv, 1, b'')

    def available(self, target):
        if self.schema_root is None or (self.schema_root / target.lstrip('/')).is_dir():
            return True
        if self.schema_maps.is_dir():
            for mapping in sorted(self.schema_maps.iterdir()):
                if not mapping.is_file():
                    continue
                for line in mapping.read_text().splitlines():
                    if not line.strip() or line.startswith('#') or ':' not in line:
                        continue
                    base, pattern = line.split(':', 1)
                    if re.fullmatch(pattern, target) and (self.schema_root / base.lstrip('/')).is_dir():
                        return True
        LOG.debug('No installed iconf schema for %s; skipping', target)
        return False

    def apply(self, records, failed_hooks=()):
        requirements = {}
        groups = {}
        targets = {}
        success = True
        for record in records:
            kind, service, *args = record
            if kind == 'requires':
                requirements.setdefault(service, set()).update(args)
                continue
            group = groups.setdefault(service, {'mode': 'reload', 'paths': set(), 'validators': [],
                                                'action': None, 'policy': 'keep', 'blocked': False, 'always': False})
            if kind in ('config', 'watch', 'remove'):
                mode, *paths = args
                if mode == 'restart':
                    group['mode'] = mode
                for target in paths:
                    if kind == 'config' and not self.available(target):
                        continue
                    group['paths'].add(target)
                    item = targets.setdefault(target, {'kind': kind, 'services': set()})
                    item['services'].add(service)
                    if kind == 'config':
                        item['kind'] = kind
            elif kind == 'validate':
                if args not in group['validators']:
                    group['validators'].append(args)
            elif kind in ('action', 'always'):
                group['action'] = args
                group['always'] = kind == 'always'
            elif kind == 'state':
                group['policy'] = args[0]
            else:
                raise ValueError('Unknown hook declaration: ' + kind)

        for service, group in groups.items():
            if group['policy'] != 'stop' and requirements.get(service, set()).intersection(failed_hooks):
                group['blocked'] = True
                success = False

        def dirty(services):
            for service in services:
                self.state['pending'][service] = True
            self.save()  # Commit intent before changing a file or invoking a daemon.

        for target, item in targets.items():
            if any(groups[service]['blocked'] for service in item['services']):
                for service in item['services']:
                    groups[service]['blocked'] = True
                success = False
                continue
            before = digest(target)
            if item['kind'] == 'config':
                result = self.command(['iconf', 'gen', target], stdout=subprocess.PIPE)
                if result.returncode:
                    for service in item['services']:
                        groups[service]['blocked'] = True
                    success = False
                    continue
                if before != hashlib.sha256(result.stdout).hexdigest():
                    dirty(item['services'])
                    if self.command(['iconf', 'repair', '-q', target]).returncode:
                        for service in item['services']:
                            groups[service]['blocked'] = True
                        success = False
                        continue
            elif item['kind'] == 'remove' and Path(target).exists():
                dirty(item['services'])
                Path(target).unlink()
            current = digest(target)
            for service in item['services']:
                known = self.state['hashes'].get(service, {})
                if target not in known or known[target] != current:
                    dirty([service])

        snapshots = {service: {target: digest(target) for target in group['paths']}
                     for service, group in groups.items()}
        for service, group in groups.items():
            if group['blocked']:
                continue
            if group['always']:
                dirty([service])
            policy = group['policy']
            active = False
            if not service.startswith('@'):
                active = self.command(['systemctl', 'is-active', '--quiet', service]).returncode == 0
            if policy == 'stop':
                if active:
                    dirty([service])
            elif policy == 'start' and not active:
                if self.command(['systemctl', 'is-enabled', '--quiet', service]).returncode == 0:
                    dirty([service])
                    policy = 'start'
                else:
                    policy = 'keep'
            else:
                policy = 'keep'
            if service not in self.state['pending']:
                continue
            if policy != 'stop':
                if any(self.command(argv).returncode for argv in group['validators']):
                    success = False
                    continue
            if policy == 'stop':
                argv = ['systemctl', 'stop', service] if active else None
            elif policy == 'start':
                argv = ['systemctl', 'start', service]
            elif active or service.startswith('@'):
                argv = group['action'] or ['systemctl', group['mode'], service]
            else:
                argv = None  # Keep intentionally inactive services stopped.
            if argv and self.command(argv).returncode:
                success = False
                continue
            self.state['hashes'][service] = snapshots[service]
            self.state['pending'].pop(service, None)
            self.save()
        return success


def collect_and_apply(hooks_dir, state_dir):
    runner = HookRunner(state_dir)
    hooks = [p for p in sorted(Path(hooks_dir).glob('*'))
             if p.is_file() and os.access(p, os.X_OK) and re.fullmatch(r'[A-Za-z0-9_-]+', p.name)]
    records = []
    success = True
    failed_hooks = set()
    with tempfile.TemporaryDirectory(prefix='hooks-', dir=state_dir) as tmp:
        for phase in ('prepare', 'configure'):
            for number, hook in enumerate(hooks):
                requests = Path(tmp) / str(number)
                requests.write_text('')
                env = dict(os.environ, PATH=PATH, IPV6CHDETECTD_PHASE=phase,
                           IPV6CHDETECTD_REQUESTS=str(requests), IPV6CHDETECTD_RUNNING='1')
                result = runner.command([str(hook)], env=env, cwd='/')
                if result.returncode:
                    LOG.error('Hook %s failed during %s', hook, phase)
                    failed_hooks.add(hook.name)
                    success = False
                if phase == 'configure':
                    try:
                        own_records = [json.loads(line) for line in requests.read_text().splitlines()]
                    except (ValueError, TypeError):
                        LOG.exception('Invalid declarations from %s', hook)
                        failed_hooks.add(hook.name)
                        success = False
                        continue
                    records.extend(own_records)
                    records.extend(['requires', record[1], hook.name]
                                   for record in own_records if record[0] != 'requires')
        return runner.apply(records, failed_hooks) and success
