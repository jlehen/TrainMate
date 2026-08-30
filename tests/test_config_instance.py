"""TRAINMATE_CONFIG + `database:` / `science_dir:` resolution (ARCHITECTURE.md §9).

The env var is read once, at import of trainmate.config, so every case here runs a fresh
interpreter via subprocess instead of reaching into module state. That is the honest
shape: each case exercises exactly what `TRAINMATE_CONFIG=... ./tm` does. The invariant
under test is the isolation rule — an instance's paths resolve beside its config file,
and an explicitly named config that cannot load aborts rather than falling back to
defaults that point at the primary athlete's database.
"""
import os
import subprocess
import sys
import tempfile
import unittest

import trainmate.config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(trainmate.config.__file__)))

# Prints the five resolved paths, one per line, from a fresh interpreter.
_PRINT_PATHS = (
    "from trainmate.config import config, CONFIG_PATH; "
    "print(CONFIG_PATH); print(config.db_path); print(config.service_account_file); "
    "print(config.science_dir); print(config.garmin_token_dir)"
)


def _run(env_config):
    """Runs _PRINT_PATHS in a fresh interpreter with TRAINMATE_CONFIG set (None = unset)."""
    env = dict(os.environ)
    env.pop("TRAINMATE_CONFIG", None)
    if env_config is not None:
        env["TRAINMATE_CONFIG"] = env_config
    return subprocess.run([sys.executable, "-c", _PRINT_PATHS], capture_output=True,
                          text=True, cwd=REPO_ROOT, env=env)


class TestInstanceSelection(unittest.TestCase):
    def _paths(self, env_config):
        proc = _run(env_config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()

    def test_env_selects_config_and_paths_resolve_beside_it(self):
        # No `database:` key: the default trainmate.db lands beside the named config, not
        # beside the code — a bare second-instance config can never open the primary DB.
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("user_profile:\n  name: Other\n")
            config_path, db_path, sa_path, science_dir, _ = self._paths(cfg)
            self.assertEqual(config_path, cfg)
            self.assertEqual(db_path, os.path.join(d, "trainmate.db"))
            self.assertEqual(sa_path, os.path.join(d, "service_account.json"))
            # Guidelines follow the same rule as the database: a second athlete inherits
            # the primary's training philosophy only by asking for it (§9).
            self.assertEqual(science_dir, os.path.join(d, "science"))

    def test_relative_database_key_resolves_beside_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("database: other.db\n")
            _, db_path, _, _, _ = self._paths(cfg)
            self.assertEqual(db_path, os.path.join(d, "other.db"))

    def test_absolute_database_key_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("database: /somewhere/else/other.db\n")
            _, db_path, _, _, _ = self._paths(cfg)
            self.assertEqual(db_path, "/somewhere/else/other.db")

    def test_relative_science_dir_key_resolves_beside_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("science_dir: guidelines\n")
            _, _, _, science_dir, _ = self._paths(cfg)
            self.assertEqual(science_dir, os.path.join(d, "guidelines"))

    def test_absolute_science_dir_key_is_respected(self):
        # How two athletes deliberately share one philosophy — the only way they can.
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("science_dir: /shared/science\n")
            _, _, _, science_dir, _ = self._paths(cfg)
            self.assertEqual(science_dir, "/shared/science")

    def test_relative_garmin_token_dir_resolves_beside_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("garmin:\n  token_dir: .garminconnect\n")
            _, _, _, _, token_dir = self._paths(cfg)
            self.assertEqual(token_dir, os.path.join(d, ".garminconnect"))

    def test_absolute_garmin_token_dir_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("garmin:\n  token_dir: /somewhere/tokens\n")
            _, _, _, _, token_dir = self._paths(cfg)
            self.assertEqual(token_dir, "/somewhere/tokens")

    def test_default_garmin_token_dir_resolves_beside_config(self):
        # Tokens beat credentials in garminconnect, so a shared default store would let
        # a second instance resume the first account's session — the default lands
        # beside the config like every other instance path; only configs sharing one
        # directory still need an explicit token_dir (DESIGN_garmin_direct_pull.md §11
        # rev. 3).
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("user_profile:\n  name: Other\n")
            _, _, _, _, token_dir = self._paths(cfg)
            self.assertEqual(token_dir, os.path.join(d, ".garminconnect"))

    def test_missing_explicit_config_aborts(self):
        proc = _run("/nonexistent/nowhere/config.yaml")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TRAINMATE_CONFIG", proc.stderr)

    def test_unparseable_explicit_config_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.yaml")
            with open(cfg, "w") as f:
                f.write("database: [unclosed\n")
            proc = _run(cfg)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("TRAINMATE_CONFIG", proc.stderr)

    def test_unset_env_keeps_the_repo_default(self):
        # Asserts only on the config *path* — the primary install's config content (and
        # therefore its db_path) belongs to the user, not to this test.
        proc = _run(None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config_path, _, _, science_dir, _ = proc.stdout.strip().splitlines()
        self.assertEqual(config_path, os.path.join(REPO_ROOT, "config.yaml"))
        # The primary install keeps the pre-`science_dir:` location, key or no key.
        self.assertEqual(science_dir, os.path.join(REPO_ROOT, "science"))


if __name__ == "__main__":
    unittest.main()
