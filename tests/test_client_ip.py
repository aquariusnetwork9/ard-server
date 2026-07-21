"""Client-IP resolution: a trusted local reverse proxy may supply the real client
address via header; an untrusted peer's header claims are ignored; IPv6 addresses
bucket at /64 so per-address rate limiting/corroboration stays meaningful."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import (  # noqa: E402
    App, Auth, Store, DEFAULT_TRUSTED_PROXIES, _normalize_ip, _forwarded_ip,
)
import trust  # noqa: E402

GEO_DIR = ROOT / "geometry"


def _app(trusted_proxies=None):
    store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-clientip")
    auth = Auth(trust.Registry())
    return App(store, auth, trusted_proxies=trusted_proxies)


class NormalizeIpTests(unittest.TestCase):
    def test_ipv4_passes_through(self):
        self.assertEqual(_normalize_ip("203.0.113.7"), "203.0.113.7")

    def test_ipv6_collapses_to_64(self):
        self.assertEqual(_normalize_ip("2001:db8:abcd:1234::1"), "2001:db8:abcd:1234::")
        # A different address in the same /64 must collapse to the identical key --
        # otherwise one allocation looks like unlimited distinct addresses.
        self.assertEqual(_normalize_ip("2001:db8:abcd:1234::2"), "2001:db8:abcd:1234::")

    def test_ipv6_different_64_blocks_stay_distinct(self):
        self.assertNotEqual(_normalize_ip("2001:db8:abcd:1234::1"),
                             _normalize_ip("2001:db8:abcd:5678::1"))

    def test_unparseable_passes_through(self):
        self.assertEqual(_normalize_ip("?"), "?")


class ForwardedIpTests(unittest.TestCase):
    def test_cf_connecting_ip_wins(self):
        self.assertEqual(
            _forwarded_ip({"CF-Connecting-IP": "198.51.100.9", "X-Forwarded-For": "198.51.100.1"}),
            "198.51.100.9")

    def test_falls_back_to_x_forwarded_for_last_entry(self):
        self.assertEqual(_forwarded_ip({"X-Forwarded-For": "198.51.100.1, 198.51.100.2"}),
                          "198.51.100.2")

    def test_no_headers_returns_none(self):
        self.assertIsNone(_forwarded_ip({}))


class TrustsPeerTests(unittest.TestCase):
    def test_loopback_trusted_by_default(self):
        app = _app()
        self.assertTrue(app.trusts_peer("127.0.0.1"))
        self.assertTrue(app.trusts_peer("::1"))

    def test_arbitrary_peer_not_trusted_by_default(self):
        app = _app()
        self.assertFalse(app.trusts_peer("203.0.113.7"))

    def test_explicit_empty_disables_even_loopback(self):
        app = _app(trusted_proxies=[])
        self.assertFalse(app.trusts_peer("127.0.0.1"))

    def test_explicit_list_widens_trust(self):
        app = _app(trusted_proxies=list(DEFAULT_TRUSTED_PROXIES) + ["10.0.0.5/32"])
        self.assertTrue(app.trusts_peer("10.0.0.5"))
        self.assertFalse(app.trusts_peer("10.0.0.6"))

    def test_unparseable_peer_is_never_trusted(self):
        app = _app()
        self.assertFalse(app.trusts_peer("?"))


if __name__ == "__main__":
    unittest.main()
