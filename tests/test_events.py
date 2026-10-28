import importlib.util
import pathlib
import socket
import struct
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / 'usr/lib/iserv/ipv6chdetectd/events.py'

def load():
    spec = importlib.util.spec_from_file_location('events', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class EventsTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'event-driven implementation is missing')
        self.events = load()

    def test_fixed_window_and_events_during_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = self.events.EventQueue(pathlib.Path(tmp), 3)
            q.notify(now=10)
            q.notify(now=12)
            self.assertFalse(q.ready(12.9))
            self.assertTrue(q.ready(13))
            q.claim()
            q.notify(now=14)
            q.notify(now=15)
            q.finish(True)
            self.assertFalse(q.ready(16.9))
            self.assertTrue(q.ready(17))
            q.claim()
            q.finish(True)
            self.assertFalse(q.ready(100))

    def test_inflight_survives_restart_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = self.events.EventQueue(pathlib.Path(tmp), 0)
            q.notify()
            q.claim()
            q = self.events.EventQueue(pathlib.Path(tmp), 0)
            self.assertTrue(q.ready())
            q.claim()
            q.finish(False)
            self.assertTrue(q.ready())

    def test_ipv6_address_lifetime_refresh_is_ignored(self):
        def attr(kind, data):
            size = 4 + len(data)
            return struct.pack('HH', size, kind) + data + b'\0' * (-size % 4)
        def msg(family, lifetime):
            body = struct.pack('BBBBI', family, 64, 0, 0, 2)
            body += attr(1, socket.inet_pton(socket.AF_INET6, '2001:db8::1'))
            body += attr(6, struct.pack('IIII', lifetime, lifetime, 0, 0))
            return struct.pack('IHHII', len(body) + 16, 20, 0, 0, 0) + body
        monitor = self.events.NetlinkFilter()
        self.assertFalse(monitor.changed(msg(socket.AF_INET, 300)))
        self.assertTrue(monitor.changed(msg(socket.AF_INET6, 300)))
        self.assertFalse(monitor.changed(msg(socket.AF_INET6, 299)))
        deleted = bytearray(msg(socket.AF_INET6, 299))
        struct.pack_into('H', deleted, 4, 21)
        self.assertTrue(monitor.changed(deleted))

    def test_route_lifetimes_and_cloned_routes_do_not_trigger(self):
        def message(expires, flags=0):
            body = struct.pack('BBBBBBBBI', socket.AF_INET6, 0, 0, 0, 254, 9, 0, 1, flags)
            body += struct.pack('HHI', 8, 23, expires)
            return struct.pack('IHHII', len(body)+16, 24, 0, 0, 0)+body
        monitor = self.events.NetlinkFilter()
        self.assertTrue(monitor.changed(message(300)))
        self.assertFalse(monitor.changed(message(299)))
        self.assertFalse(monitor.changed(message(299, 0x200)))

    def test_untrusted_queue_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            directory.chmod(0o777)
            with self.assertRaises(PermissionError):
                self.events.EventQueue(directory)

    def test_clock_adjustment_does_not_extend_a_batch(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            q = self.events.EventQueue(pathlib.Path(tmp), 3)
            q.notify(now=100)
            with patch.object(self.events.time, 'time', return_value=100), patch.object(self.events.time, 'monotonic', return_value=10):
                self.assertFalse(q.ready())
            with patch.object(self.events.time, 'time', return_value=50), patch.object(self.events.time, 'monotonic', return_value=14):
                self.assertTrue(q.ready())

if __name__ == '__main__':
    unittest.main()
