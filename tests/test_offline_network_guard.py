import socket
import unittest
from validate_offline import offline_network_guard


class OfflineNetworkGuardTests(unittest.TestCase):
    def test_external_and_hostname_connections_are_blocked(self):
        with offline_network_guard():
            for host in ('example.com', '10.200.48.146', '127.0.0.2'):
                with self.assertRaises(AssertionError):
                    socket.create_connection((host, 80), timeout=.1)

    def test_literal_loopback_is_allowed_for_fake_servers(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            with offline_network_guard(), socket.create_connection(server.getsockname(), timeout=1) as client:
                peer, _ = server.accept()
                peer.close()
                self.assertEqual(client.getpeername(), server.getsockname())


if __name__ == '__main__':
    unittest.main()
