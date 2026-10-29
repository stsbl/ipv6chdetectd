"""Foreground event ingestion; hook execution never blocks netlink reception."""
import argparse
import fcntl
import logging
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time

from events import EventQueue, NetlinkFilter
from hooks import collect_and_apply

LOG = logging.getLogger(__name__)
STATE = '/var/lib/iserv/ipv6chdetectd'
HOOKS = '/usr/lib/iserv/ipv6chdetectd.d'


def notify(directory=STATE):
    directory = Path(directory)
    queue = EventQueue(directory, recover=False)
    storage_error = None
    try:
        queue.notify()
    except OSError as error:
        # A running client may still have its old read-only mount namespace
        # after the package installed our ReadWritePaths drop-in. Let the
        # worker persist the event through the socket in that case.
        storage_error = error
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wake:
        wake.setblocking(False)
        try:
            wake.sendto(b'change', str(directory / 'notify.sock'))
        except OSError:
            if storage_error is not None:
                raise storage_error
            # Otherwise the durable marker is consumed on worker recovery.



def serve(directory=STATE, hooks_dir=HOOKS, debounce=3, monitor=True):
    directory = Path(directory)
    queue = EventQueue(directory, debounce, recover=False)
    with (directory / 'daemon.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only the lock owner recovers an interrupted pass.
        if queue.inflight.exists():
            queue.notify()
            queue.inflight.unlink()
        queue.notify()  # Reconcile current state once at every startup.
        sockname = directory / 'notify.sock'
        sockname.unlink(missing_ok=True)
        running = True
        def stop(_signum, _frame):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        worker = None
        worker_started = None
        retry_at = 0
        retry_delay = 3
        filter_events = NetlinkFilter()
        with selectors.DefaultSelector() as selector, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wake:
            wake.bind(str(sockname))
            os.chmod(sockname, 0o600)
            wake.setblocking(False)
            selector.register(wake, selectors.EVENT_READ, 'notify')
            netlink = None
            if monitor:
                netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
                netlink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
                netlink.bind((0, 1 | 0x100 | 0x400))  # LINK, IPV6_IFADDR, IPV6_ROUTE
                netlink.setblocking(False)
                selector.register(netlink, selectors.EVENT_READ, 'netlink')
            try:
                while running:
                    for key, _ in selector.select(0.1):
                        try:
                            data = key.fileobj.recv(1024 * 1024)
                        except BlockingIOError:
                            continue
                        except OSError:
                            LOG.exception('Event reception failed; scheduling reconciliation')
                            queue.notify()
                            continue
                        if key.data == 'notify' or filter_events.changed(data):
                            queue.notify()
                    if worker is not None and worker.poll() is not None:
                        success = worker.returncode == 0
                        queue.finish(success)
                        elapsed = time.monotonic() - worker_started
                        if success:
                            LOG.info('[pass] Completed in %.2fs; %s', elapsed,
                                     'more events queued' if queue.pending.exists() else 'queue empty')
                            retry_delay = 3
                            retry_at = 0
                        else:
                            retry_at = time.monotonic() + retry_delay
                            LOG.error('[pass] Failed after %.2fs; retry in %s seconds', elapsed, retry_delay)
                            retry_delay = min(retry_delay * 2, 60)
                        worker = None
                    if worker is None and queue.ready() and time.monotonic() >= retry_at:
                        queue.claim()
                        worker_started = time.monotonic()
                        LOG.info('[pass] Starting collected IPv6 update')
                        try:
                            worker = subprocess.Popen(
                                [sys.executable, __file__, '--run-hooks', '--state-dir', str(directory),
                                 '--hooks-dir', str(hooks_dir)], start_new_session=True)
                        except OSError:
                            LOG.exception('Could not launch hook pass')
                            queue.finish(False)
                            retry_at = time.monotonic() + retry_delay
            finally:
                if worker is not None and worker.poll() is None:
                    os.killpg(worker.pid, signal.SIGTERM)
                    try:
                        worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(worker.pid, signal.SIGKILL)
                        worker.wait()
                    # Keep inflight for startup recovery, even if shutdown interrupted a hook.
                if netlink is not None:
                    netlink.close()
                sockname.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Apply IPv6 changes through debounced package hooks')
    parser.add_argument('--notify', action='store_true', help='queue a change and return immediately')
    parser.add_argument('-d', '--daemon', action='store_true', help='compatibility option; run in foreground')
    parser.add_argument('--run-hooks', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--state-dir', default=STATE)
    parser.add_argument('--hooks-dir', default=HOOKS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    if args.notify:
        notify(args.state_dir)
        return 0
    if args.run_hooks:
        with (Path(args.state_dir) / 'hooks.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return 0 if collect_and_apply(args.hooks_dir, args.state_dir) else 1
    serve(args.state_dir, args.hooks_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
