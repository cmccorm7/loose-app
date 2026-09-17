"""The launcher: picking a port and refusing to listen beyond this machine."""

import socket
import unittest

from hub.__main__ import free_port, main


class TestPortSelection(unittest.TestCase):
    def test_a_free_port_is_used_as_asked(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            candidate = probe.getsockname()[1]
        self.assertEqual(free_port(candidate), candidate)

    def test_a_busy_port_falls_back_to_another(self):
        with socket.socket() as taken:
            taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            busy = taken.getsockname()[1]
            chosen = free_port(busy)
        self.assertNotEqual(chosen, busy)
        self.assertGreater(chosen, 0)

    def test_the_chosen_port_is_actually_bindable(self):
        port = free_port(0)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))


class TestLauncherArguments(unittest.TestCase):
    def test_bad_arguments_exit_rather_than_raise(self):
        with self.assertRaises(SystemExit):
            main(["--port", "not-a-number"])

    def test_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)


class TestBindAddress(unittest.TestCase):
    def test_the_server_is_started_on_loopback_only(self):
        """A trading journal must not be reachable from the network."""
        import inspect
        from hub import __main__ as launcher
        source = inspect.getsource(launcher.main)
        self.assertIn('host="127.0.0.1"', source)
        self.assertNotIn('host="0.0.0.0"', source)


if __name__ == "__main__":
    unittest.main()
