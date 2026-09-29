"""Run: python3 -m unittest discover -s seeders/cargo -p 'test_*.py'."""

import contextlib
import importlib.util
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("seed_cargo", Path(__file__).with_name("seed-cargo.py"))
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)


class CargoSeederTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.args = SimpleNamespace(
            dependency=None, toml_file=None, project_dir=None, package=None,
            cargo="cargo", allow_dirty=False, external_repo="cargo-external",
            internal_repo="cargo-internal", virtual_repo="cargo", command_timeout=10,
        )

    def test_index_paths(self):
        for name, path in [("a", "1/a"), ("ab", "2/ab"), ("abc", "3/a/abc"), ("Serde", "se/rd/serde")]:
            self.assertEqual(SEED.index_path(name), path)

    def test_dependency_requires_exact_requested_version(self):
        self.args.dependency = "serde@1.0.229"
        manifest = SEED.prepare_manifest(self.args, self.work)
        self.assertIn('serde = "=1.0.229"', manifest.read_text())

    def test_fragment_rejects_relative_paths(self):
        self.args.toml_file = self.work / "dependencies.toml"
        self.args.toml_file.write_text('[dependencies]\nother = { path = "../other" }\n')
        with self.assertRaisesRegex(RuntimeError, "Path/workspace"):
            SEED.prepare_manifest(self.args, self.work)

    def test_publish_payload_preserves_bytes_aliases_and_features(self):
        crate = b"original compressed crate bytes"
        entry = {
            "name": "example", "vers": "1.0.0",
            "deps": [{"name": "renamed", "package": "original", "req": "^1", "kind": "build", "target": "cfg(unix)", "optional": True, "default_features": False, "features": ["std"]}],
            "features": {"default": []}, "features2": {"extra": ["dep:renamed", "renamed?/std"]},
        }
        data = SEED.publish_payload(entry, crate)
        length = struct.unpack("<I", data[:4])[0]
        metadata = json.loads(data[4:4 + length])
        self.assertEqual(metadata["deps"], entry["deps"])
        self.assertEqual(metadata["features"]["extra"], entry["features2"]["extra"])
        self.assertEqual(struct.unpack("<I", data[4 + length:8 + length])[0], len(crate))
        self.assertEqual(data[8 + length:], crate)

    def mirror(self, existing=None):
        crate = b"unchanged .crate archive"
        checksum = SEED.sha256(crate)
        entry = {"name": "example", "vers": "1.0.0", "cksum": checksum, "deps": [], "features": {}}
        cache = self.work / "home/registry/cache/upstream"
        cache.mkdir(parents=True)
        (cache / "example-1.0.0.crate").write_bytes(crate)
        (self.work / "Cargo.lock").write_text(f'[[package]]\nname = "example"\nversion = "1.0.0"\nsource = "{SEED.CRATES_IO_SOURCE}"\nchecksum = "{checksum}"\n')
        client = SimpleNamespace(published=existing, puts=0)
        client.url = lambda repo: f"https://localhost/cargo/{repo}/"
        client.entry = lambda *args: client.published

        def request(url, *, local=True, data=None):
            if not local:
                return json.dumps(entry).encode()
            if data is not None:
                client.puts += 1
                size = struct.unpack("<I", data[:4])[0]
                self.assertEqual(data[8 + size:], crate)
                client.published = entry
                return b'{}'
            return crate

        client.request = request
        with patch.object(SEED, "metadata", return_value={"workspace_root": str(self.work)}), patch.object(SEED, "run_command") as command, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = SEED.mirror_dependencies(self.args, self.work / "Cargo.toml", client, self.work, {"CARGO_HOME": str(self.work / "home")})
        self.assertIn("--locked", command.call_args.args[0])
        return result, client, checksum

    def test_mirror_uploads_original_bytes_and_verifies_download(self):
        result, client, _ = self.mirror()
        self.assertEqual(result, (1, 0, 0))
        self.assertEqual(client.puts, 1)

    def test_existing_mirror_is_successful_skip(self):
        checksum = SEED.sha256(b"unchanged .crate archive")
        result, client, _ = self.mirror({"cksum": checksum})
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(client.puts, 0)

    def test_conflicting_mirror_is_not_overwritten(self):
        result, client, _ = self.mirror({"cksum": "0" * 64})
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(client.puts, 0)

    def test_internal_duplicate_does_not_build_or_publish(self):
        manifest = self.work / "Cargo.toml"
        package = {"name": "internal", "version": "1.0.0", "manifest_path": str(manifest), "publish": ["cargo-internal"]}
        client = SimpleNamespace(entry=lambda *args: {"vers": "1.0.0"})
        with patch.object(SEED, "metadata", return_value={"packages": [package]}), patch.object(SEED, "run_command") as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SEED.publish_project(self.args, manifest, client, self.work, {}), (0, 1, 0))
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
