"""Run: python3 -m unittest discover -s seeders/maven -p 'test_*.py'."""

import contextlib
import importlib.util
import io
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location("seed_maven", Path(__file__).with_name("seed-maven.py"))
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)
POM = b'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>example.ak</groupId><artifactId>demo</artifactId><version>1.0</version><dependencies><dependency><groupId>a</groupId><artifactId>b</artifactId><version>2</version></dependency></dependencies></project>'


class Client:
    def __init__(self, files=None):
        self.files = files or {}
        self.puts = []

    def request(self, path, data=None):
        if data is not None:
            self.files[path] = data
            self.puts.append(path)
            return b""
        return self.files.get(path)


class SeederTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.args = SimpleNamespace(jar=self.work/'demo.jar', pom_file=None, group_id=None, artifact_id=None, version=None, classifier=None)

    def jar(self, embedded=None):
        with zipfile.ZipFile(self.args.jar, 'w') as archive:
            archive.writestr('example/Demo.class', b'fixture')
            if embedded:
                archive.writestr('META-INF/maven/example.ak/demo/pom.xml', embedded)

    def test_embedded_pom_preserves_dependencies_and_original_jar(self):
        self.jar(POM)
        files = SEED.prepare_jar(self.args)
        self.assertEqual(files[0], ('example/ak/demo/1.0/demo-1.0.pom', POM))
        self.assertEqual(files[1][1], self.args.jar.read_bytes())

    def test_generated_pom_and_classifier(self):
        self.jar()
        self.args.group_id, self.args.artifact_id, self.args.version = 'a.b', 'demo', '1.0'
        self.args.classifier = 'sources'
        with contextlib.redirect_stdout(io.StringIO()):
            files = SEED.prepare_jar(self.args)
        self.assertEqual(files[1][0], 'a/b/demo/1.0/demo-1.0-sources.jar')

    def test_rejects_missing_and_ambiguous_coordinates(self):
        self.jar()
        with self.assertRaises(ValueError):
            SEED.prepare_jar(self.args)
        self.args.group_id = 'example'
        with self.assertRaises(ValueError):
            SEED.prepare_jar(self.args)

    def test_unresolved_version_and_snapshot_rejected(self):
        for version in ('${revision}', '1-SNAPSHOT', '../escape'):
            with self.assertRaises(ValueError):
                SEED.coordinates('a', 'b', version)

    def test_cache_includes_parent_bom_classifier_excludes_tracking(self):
        for filename in ('demo-1.0.pom', 'demo-1.0.jar', 'demo-1.0-tests.jar', '_remote.repositories', 'demo-1.0.jar.sha1', 'demo-1.0.jar.lastUpdated', 'maven-metadata-upstream.xml'):
            path = self.work/'a/demo/1.0'/filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        self.assertEqual(len(SEED.downloaded_artifacts(self.work)), 3)

    def test_snapshot_cache_rejected_before_upload(self):
        path = self.work/'a/demo/1.0-SNAPSHOT/demo-1.0-SNAPSHOT.jar'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture')
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT'):
            SEED.downloaded_artifacts(self.work)

    def test_explicit_pom_coordinates_must_match(self):
        self.jar()
        self.args.pom_file = self.work/'pom.xml'
        self.args.pom_file.write_bytes(POM)
        self.args.group_id, self.args.artifact_id, self.args.version = 'wrong', 'demo', '1.0'
        with self.assertRaisesRegex(ValueError, 'disagree'):
            SEED.prepare_jar(self.args)

    def test_jar_zip_suffix_rejected(self):
        self.args.jar = self.work/'demo.jar.zip'
        self.jar(POM)
        with self.assertRaisesRegex(ValueError, '.jar'):
            SEED.prepare_jar(self.args)

    def test_unsafe_repository_urls_rejected(self):
        for url in ('ftp://localhost/repo', 'https://user:password@localhost/repo', 'https://localhost/repo?token=secret'):
            with self.assertRaises(ValueError):
                SEED.validate_url(url)

    def test_existing_files_skip_even_when_different(self):
        client = Client({'a.jar': b'old'})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SEED.upload_artifacts(client, [('a.jar', b'new')]), (0, 1, 0))
        self.assertEqual(client.puts, [])

    def test_partial_publication_repairs_missing_pom(self):
        client = Client({'a.jar': b'jar'})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SEED.upload_artifacts(client, [('a.pom', b'pom'), ('a.jar', b'jar')]), (1, 1, 0))
        self.assertEqual(client.puts, ['a.pom'])

    def test_authentication_error_is_failure_not_missing(self):
        class Denied(Client):
            def request(self, path, data=None):
                raise urllib.error.HTTPError('https://localhost', 403, 'denied', {}, None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(SEED.upload_artifacts(Denied(), [('a.jar', b'new')]), (0, 0, 1))

    def test_corrupt_readback_is_failure(self):
        class Corrupt(Client):
            def request(self, path, data=None):
                if data is not None:
                    return super().request(path, b'corrupt')
                return super().request(path)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(SEED.upload_artifacts(Corrupt(), [('a.jar', b'new')]), (0, 0, 1))

    def test_concurrent_upload_skips(self):
        class Race(Client):
            def request(self, path, data=None):
                if data is not None:
                    self.files[path] = data
                    raise urllib.error.HTTPError('https://localhost', 409, 'conflict', {}, None)
                return super().request(path)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(SEED.upload_artifacts(Race(), [('a.jar', b'new')]), (0, 1, 0))


if __name__ == '__main__':
    unittest.main()
