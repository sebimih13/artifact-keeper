"""Run with: python3 -m unittest discover -s seeders/p2 -p 'test_*.py'."""

import contextlib
import hashlib
import importlib.util
import io
import json
import lzma
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location("seed_p2", Path(__file__).with_name("seed-p2.py"))
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)


class SeederTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.site = Path(self.work.name)
        self.remote = {}
        self.puts = []
        self.fail_path = None
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/api/v1/repositories/p2":
                    body = json.dumps({"format": "generic", "repo_type": "local", "versioning_enabled": True}).encode()
                else:
                    key = self.path.split("/download/", 1)[-1]
                    body = test.remote.get(key)
                    if body is None:
                        self.send_error(404)
                        return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                key = self.path.split("/artifacts/", 1)[-1]
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if key == test.fail_path:
                    self.send_error(500)
                    return
                if self.headers.get("Authorization") != "Bearer test-token" or self.headers.get("X-Checksum-Sha256") != hashlib.sha256(body).hexdigest():
                    self.send_error(400)
                    return
                test.remote[key] = body
                test.puts.append(key)
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.args = SimpleNamespace(
            output_dir=self.site, site_path="releases/1.0", http_timeout=5,
            ca_bundle=Path(SEED.DEFAULT_CA_BUNDLE),
            artifact_keeper=f"http://127.0.0.1:{self.server.server_port}/api/v1/repositories/p2",
        )
        self.paths = [Path("plugins/example_1.0.0.jar")] + [Path(name) for name in SEED.METADATA_FILES]
        for path in self.paths:
            (self.site / path).parent.mkdir(parents=True, exist_ok=True)
            (self.site / path).write_bytes(path.as_posix().encode())

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def upload(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return SEED.upload_site(self.args, self.paths, "test-token")

    def test_initial_repeat_and_new_plugin(self):
        self.assertEqual(self.upload(), 0)
        self.assertEqual(self.puts, ["releases/1.0/" + path.as_posix() for path in self.paths])
        self.puts.clear()
        self.assertEqual(self.upload(), 0)
        self.assertEqual(self.puts, [])
        plugin = Path("plugins/new_1.0.0.jar")
        (self.site / plugin).write_bytes(b"new bundle")
        self.paths.insert(1, plugin)
        for name in SEED.METADATA_FILES[:-1]:
            (self.site / name).write_bytes(b"updated metadata")
        self.assertEqual(self.upload(), 0)
        self.assertEqual(self.puts, ["releases/1.0/" + name for name in [plugin.as_posix(), *SEED.METADATA_FILES[:-1]]])

    def test_changed_published_jar_aborts_before_any_upload(self):
        self.remote["releases/1.0/plugins/example_1.0.0.jar"] = b"other bytes"
        with self.assertRaisesRegex(RuntimeError, "Increment"):
            self.upload()
        self.assertEqual(self.puts, [])

    def test_artifact_failure_withholds_metadata(self):
        self.fail_path = "releases/1.0/plugins/example_1.0.0.jar"
        self.assertEqual(self.upload(), 1)
        self.assertEqual(self.puts, [])

    def test_metadata_failure_stops_later_metadata(self):
        self.fail_path = "releases/1.0/artifacts.jar"
        self.assertEqual(self.upload(), 1)
        self.assertEqual(self.puts, ["releases/1.0/plugins/example_1.0.0.jar"])

    def test_packaging_preserves_xml_and_stable_timestamps(self):
        previous = self.site / "previous"
        previous.mkdir()
        original = b"<?xml version='1.0'?><?metadataRepository version='1.2.0'?><repository><properties><property name='p2.timestamp' value='123'/></properties></repository>"
        for name in ("artifacts", "content"):
            with zipfile.ZipFile(previous / (name + ".jar"), "w") as z:
                z.writestr(name + ".xml", original)
            with zipfile.ZipFile(self.site / (name + ".jar"), "w") as z:
                z.writestr(name + ".xml", original.replace(b"123", b"456"))
        SEED.package_metadata(self.site, previous)
        for name in ("artifacts", "content"):
            with zipfile.ZipFile(self.site / (name + ".jar")) as z:
                self.assertEqual(z.read(name + ".xml"), original)
            self.assertEqual(lzma.decompress((self.site / (name + ".xml.xz")).read_bytes()), original)


if __name__ == "__main__":
    unittest.main()
