"""Unit tests.

The behavioural checks live in radar/selftest.py so they can also run as a
single command on a fresh clone (``python3 -m radar selftest``). This module
exposes each of them as a unittest case so CI reports them individually, and
adds coverage for config validation, which selftest only smoke-checks.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from radar.config import ConfigError, load_config
from radar.selftest import CHECKS


class SelfTestChecks(unittest.TestCase):
    """Each engine check, surfaced as its own test."""


def _attach(name, check):
    def test(self):
        check()

    test.__name__ = "test_" + name.replace(" ", "_").replace("-", "_")
    test.__doc__ = check.__doc__
    setattr(SelfTestChecks, test.__name__, test)


for _name, _check in CHECKS:
    _attach(_name, _check)


class ConfigValidation(unittest.TestCase):
    """Bad config must fail loudly at load time, not mid-run."""

    def _write(self, directory: Path, **files: str) -> None:
        defaults = {
            "radar.toml": "[engine]\n",
            "watchlist.toml": (
                "[[instrument]]\nid = 'a'\nname = 'A'\nasset_class = 'equity'\n"
                "signal_symbol = 'A'\nrules = ['r']\n"
            ),
            "rules.toml": "[[rule]]\nid = 'r'\nkind = 'rsi'\nbelow = 30\n",
            "portfolio.toml": "",
        }
        defaults.update(files)
        for filename, content in defaults.items():
            (directory / filename).write_text(content, encoding="utf-8")

    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(path)
            config = load_config(path)
            self.assertEqual(len(config.instruments), 1)
            self.assertEqual(len(config.rules), 1)
            self.assertEqual(config.rules["r"].param("below"), 30)

    def test_missing_directory(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/radar-config"))

    def test_rule_reference_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(
                path,
                **{
                    "watchlist.toml": (
                        "[[instrument]]\nid = 'a'\nname = 'A'\nasset_class = 'equity'\n"
                        "signal_symbol = 'A'\nrules = ['nope']\n"
                    )
                },
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("nope", str(ctx.exception))

    def test_duplicate_instrument_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(
                path,
                **{
                    "watchlist.toml": (
                        "[[instrument]]\nid = 'a'\nname = 'A'\nasset_class = 'equity'\n"
                        "signal_symbol = 'A'\nrules = ['r']\n\n"
                        "[[instrument]]\nid = 'a'\nname = 'B'\nasset_class = 'equity'\n"
                        "signal_symbol = 'B'\nrules = ['r']\n"
                    )
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_bad_asset_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(
                path,
                **{
                    "watchlist.toml": (
                        "[[instrument]]\nid = 'a'\nname = 'A'\nasset_class = 'bonds'\n"
                        "signal_symbol = 'A'\nrules = ['r']\n"
                    )
                },
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("asset_class", str(ctx.exception))

    def test_bad_notify_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(path, **{"radar.toml": "[notify]\nmin_level = 'urgent'\n"})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_holding_must_reference_known_instrument(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(
                path,
                **{
                    "portfolio.toml": (
                        "[[holding]]\ninstrument_id = 'ghost'\nquantity = 1\n"
                        "entry_price = 10\n"
                    )
                },
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("ghost", str(ctx.exception))

    def test_malformed_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._write(path, **{"rules.toml": "[[rule]\nid = broken"})
            with self.assertRaises(ConfigError):
                load_config(path)


class CliWiring(unittest.TestCase):
    """The CLI parses its documented commands."""

    def test_parser_accepts_documented_commands(self):
        from radar.main import build_parser

        parser = build_parser()
        for argv in (
            ["check"],
            ["run"],
            ["run", "--dry-run", "--force"],
            ["snapshot"],
            ["test-alert", "--level", "high"],
            ["size", "sp500"],
            ["selftest"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(callable(args.func))


if __name__ == "__main__":
    unittest.main(verbosity=2)
