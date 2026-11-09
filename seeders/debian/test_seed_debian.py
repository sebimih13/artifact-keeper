"""Regression checks for package identity, immutable uploads and signing setup."""
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = load("seed_debian", "seed-debian.py")
demo = load("build_demo", "build-demo.py")


class FakeClient:
    def __init__(self, existing=None, conflict=None, corrupt=False):
        self.data, self.conflict, self.corrupt = existing, conflict, corrupt
        self.writes = []

    def existing(self, path):
        return self.data

    def request(self, path, method="GET", data=None, content_type=None):
        if method == "PUT":
            self.writes.append(path)
            if self.conflict is not None:
                self.data = self.conflict
                raise urllib.error.HTTPError(path, 409, "Conflict", {}, io.BytesIO())
            self.data = b"corrupt" if self.corrupt else data
        return self.data


class SeederTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = demo.build(self.root, "ak-devtools-hello", "1.0.0-1", "arm64")

    def test_deterministic_build_and_control_identity(self):
        original = self.path.read_bytes()
        self.assertEqual(demo.build(self.root, "ak-devtools-hello", "1.0.0-1", "arm64").read_bytes(), original)
        renamed = self.root / "untrusted-filename.deb"
        self.path.rename(renamed)
        client = FakeClient()
        self.assertEqual(seed.upload(client, "apt-local-devtools", "main", renamed), "uploaded")
        self.assertEqual(client.writes, ["/debian/apt-local-devtools/pool/main/a/ak-devtools-hello/ak-devtools-hello_1.0.0-1_arm64.deb"])

    def test_truncated_archive_is_rejected(self):
        self.path.write_bytes(self.path.read_bytes()[:70])
        with self.assertRaises(ValueError):
            seed.inspect_deb(self.path)

    def test_same_bytes_skip_without_upload(self):
        client = FakeClient(self.path.read_bytes())
        self.assertEqual(seed.upload(client, "repo", "main", self.path), "skipped")
        self.assertFalse(client.writes)

    def test_conflicting_bytes_are_never_overwritten(self):
        client = FakeClient(b"other-package")
        with self.assertRaisesRegex(ValueError, "Conflicting bytes"):
            seed.upload(client, "repo", "main", self.path)
        self.assertFalse(client.writes)

    def test_matching_concurrent_upload_skips(self):
        client = FakeClient(conflict=self.path.read_bytes())
        self.assertEqual(seed.upload(client, "repo", "main", self.path), "skipped")

    def test_conflicting_concurrent_upload_fails(self):
        with self.assertRaisesRegex(ValueError, "Conflicting bytes"):
            seed.upload(FakeClient(conflict=b"other"), "repo", "main", self.path)

    def test_download_verification_detects_corruption(self):
        with self.assertRaisesRegex(ValueError, "Conflicting bytes"):
            seed.upload(FakeClient(corrupt=True), "repo", "main", self.path)

    def test_wrong_repository_type_rejected(self):
        client = FakeClient(json.dumps({"format": "debian", "repo_type": "remote"}).encode())
        with self.assertRaisesRegex(ValueError, "local Debian"):
            seed.ensure_repository(client, "repo")

    def test_existing_signing_key_is_not_replaced(self):
        client = FakeClient(json.dumps({"id": "123", "format": "debian", "repo_type": "local"}).encode())
        client.json = lambda path: {"signing_key_id": "existing", "sign_metadata": False}
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            seed.ensure_repository(client, "repo", create=True)

    def test_redirects_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, "redirected"):
            seed.NoRedirect().redirect_request(None, io.BytesIO(), 302, "", {}, "https://other.invalid")


if __name__ == "__main__":
    unittest.main()
