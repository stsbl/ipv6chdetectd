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
import time

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
            except subprocess.TimeoutExpired as error:
                kill_group()
                error.output, error.stderr = process.communicate()
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

    @staticmethod
    def diagnostics(context, result, include_stdout=True, errors_only=False):
        streams = [('stderr', getattr(result, 'stderr', None))]
        if include_stdout:
            streams.append(('stdout', getattr(result, 'stdout', None)))
        for name, output in streams:
            if not output:
                continue
            text = output.decode('utf-8', errors='replace') if isinstance(output, bytes) else output
            for line in text[-65536:].splitlines():
                if errors_only and not re.search(r'(?:^|\|\s*)(?:ERROR|FATAL|CRITICAL):', line):
                    continue
                LOG.error('[%s] %s: %s', context, name, line)

    def command(self, argv, context=None, **kwargs):
        context = context or Path(argv[0]).name
        # Explicit stdout belongs to the caller (iconf's generated content),
        # and must never be dumped into the journal, even on failure.
        payload = 'stdout' in kwargs
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        probe = argv[:2] in (['systemctl', 'is-active'], ['systemctl', 'is-enabled'])
        try:
            result = self.run(argv, check=False, timeout=self.timeout, **kwargs)
            if result.returncode and not probe:
                LOG.error('[%s] %s exited with status %s', context, Path(argv[0]).name, result.returncode)
                self.diagnostics(context, result, include_stdout=not payload)
            elif not result.returncode:
                # Preserve explicit error diagnostics from tools which still
                # return success (for example Squid's ignored duplicate entry).
                self.diagnostics(context, result, include_stdout=not payload, errors_only=True)
            return result
        except subprocess.TimeoutExpired as error:
            LOG.error('[%s] %s timed out after %s seconds', context, Path(argv[0]).name, error.timeout)
            result = subprocess.CompletedProcess(argv, 1, error.output, error.stderr)
            self.diagnostics(context, result, include_stdout=not payload)
            return result
        except OSError as error:
            LOG.error('[%s] Could not execute %s: %s', context, Path(argv[0]).name, error)
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
        updated = actions = unchanged = deferred = 0
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

        LOG.info('[config] Checking %d targets for %d consumers', len(targets), len(groups))
        for target, item in targets.items():
            if any(groups[service]['blocked'] for service in item['services']):
                for service in item['services']:
                    groups[service]['blocked'] = True
                success = False
                continue
            before = digest(target)
            if item['kind'] == 'config':
                result = self.command(['iconf', 'gen', target], context='generate ' + target, stdout=subprocess.PIPE)
                if result.returncode:
                    for service in item['services']:
                        groups[service]['blocked'] = True
                    success = False
                    continue
                if before != hashlib.sha256(result.stdout).hexdigest():
                    LOG.info('[config %s] Updating for %s', target, ', '.join(sorted(item['services'])))
                    dirty(item['services'])
                    if self.command(['iconf', 'repair', '-q', target], context='update ' + target).returncode:
                        for service in item['services']:
                            groups[service]['blocked'] = True
                        success = False
                        continue
                    updated += 1
            elif item['kind'] == 'remove' and Path(target).exists():
                LOG.info('[config %s] Removing obsolete configuration', target)
                updated += 1
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
                deferred += 1
                LOG.error('[apply %s] Deferred: preparation or configuration generation failed', service)
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
                unchanged += 1
                continue
            if policy != 'stop' and group['validators']:
                LOG.info('[validate %s] Checking configuration', service)
                if any(self.command(argv, context='validate ' + service).returncode for argv in group['validators']):
                    deferred += 1
                    LOG.error('[apply %s] Deferred: configuration validation failed', service)
                    success = False
                    continue
                LOG.info('[validate %s] Configuration valid', service)
            if policy == 'stop':
                argv = ['systemctl', 'stop', service] if active else None
            elif policy == 'start':
                argv = ['systemctl', 'start', service]
            elif active or service.startswith('@'):
                argv = group['action'] or ['systemctl', group['mode'], service]
            else:
                argv = None  # Keep intentionally inactive services stopped.
            if argv:
                verb = policy if policy in ('start', 'stop') else group['mode']
                LOG.info('[apply %s] %s', service,
                         'Applying prepared configuration' if service.startswith('@') else verb.capitalize() + ' requested')
                if self.command(argv, context='apply ' + service).returncode:
                    deferred += 1
                    LOG.error('[apply %s] Failed; application remains pending for retry', service)
                    success = False
                    continue
                actions += 1
            else:
                LOG.info('[apply %s] Leaving inactive; configuration prepared for its next start', service)
            self.state['hashes'][service] = snapshots[service]
            self.state['pending'].pop(service, None)
            self.save()
            if argv:
                LOG.info('[apply %s] Completed successfully', service)
        LOG.info('[summary] %d targets checked, %d updated, %d actions applied, %d consumers unchanged, %d deferred',
                 len(targets), updated, actions, unchanged, deferred)
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
            LOG.info('[%s] Running %d package hooks', phase, len(hooks))
            for number, hook in enumerate(hooks):
                requests = Path(tmp) / str(number)
                requests.write_text('')
                env = dict(os.environ, PATH=PATH, IPV6CHDETECTD_PHASE=phase,
                           IPV6CHDETECTD_REQUESTS=str(requests), IPV6CHDETECTD_RUNNING='1')
                context = 'hook ' + phase + '/' + hook.name
                LOG.info('[%s] Running', context)
                started = time.monotonic()
                result = runner.command([str(hook)], context=context, env=env, cwd='/')
                LOG.debug('[%s] Finished in %.2fs', context, time.monotonic() - started)
                if result.returncode:
                    LOG.error('[%s] Failed', context)
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
                    LOG.debug('[%s] Registered %d declarations', context, len(own_records))
                    records.extend(own_records)
                    records.extend(['requires', record[1], hook.name]
                                   for record in own_records if record[0] != 'requires')
        return runner.apply(records, failed_hooks) and success
