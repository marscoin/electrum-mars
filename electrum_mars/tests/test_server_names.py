import json
import os
import re
import unittest

from electrum_mars import constants
from electrum_mars.network import FORMER_SERVER_ADDRESSES, migrate_server_string


class TestServerNames(unittest.TestCase):
    """The servers have certificates issued for their names. A wallet that connects by
    IP address cannot verify them and never connects (Windows report, November 2026)."""

    def test_built_in_servers_are_names_not_addresses(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "servers.json")
        with open(path) as f:
            servers = json.load(f)
        self.assertGreaterEqual(len(servers), 2)
        for host in servers:
            self.assertIsNone(re.fullmatch(r"[0-9.]+", host), host)
            self.assertNotIn(host, FORMER_SERVER_ADDRESSES)

    def test_saved_server_setting_moves_to_the_name(self):
        self.assertEqual("electrum.marscoin.org:50002:s", migrate_server_string("161.35.136.251:50002:s"))
        self.assertEqual("electrum2.marscoin.org:50002:s", migrate_server_string("147.182.177.23:50002:s"))
        self.assertEqual("electrum.marscoin.org:50002:s", migrate_server_string("164.90.138.207:50002:s"))

    def test_other_servers_are_left_alone(self):
        for s in ("example.org:50002:s", "10.0.0.5:50001:t", "electrum.marscoin.org:50002:s", None):
            self.assertEqual(s, migrate_server_string(s))

    def test_every_former_address_maps_to_a_built_in_server(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "servers.json")
        with open(path) as f:
            servers = json.load(f)
        for name in FORMER_SERVER_ADDRESSES.values():
            self.assertIn(name, servers)
