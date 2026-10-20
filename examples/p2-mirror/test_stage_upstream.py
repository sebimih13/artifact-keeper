"""Staging must reject corrupt downloads and safely reuse completed files."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('stage_upstream', Path(__file__).with_name('stage_upstream.py'))
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class StagingTests(unittest.TestCase):
    def exercise(self, corrupt=False):
        payload = b'valid artifact'
        sha = hashlib.sha256(payload).hexdigest()
        catalogs = {
            'https://example.org/site/content.xml': b'<repository><units><unit id="example" version="1"/></units></repository>',
            'https://example.org/site/artifacts.xml': f'''<repository><mappings>
              <rule output="${{repoUrl}}/plugins/${{id}}_${{version}}.jar"/>
              </mappings><artifacts><artifact classifier="osgi.bundle" id="example" version="1">
              <properties><property name="download.checksum.sha-256" value="{sha}"/></properties>
              </artifact></artifacts></repository>'''.encode(),
            'https://example.org/site/plugins/example_1.jar': b'bad bytes' if corrupt else payload,
        }
        def get(url):
            if url not in catalogs:
                raise HTTPError(url, 404, 'Not found', {}, None)
            return io.BytesIO(catalogs[url])
        with tempfile.TemporaryDirectory() as tmp, patch.object(staging, 'get', side_effect=get) as mocked:
            if corrupt:
                with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
                    staging.stage('https://example.org/site/', Path(tmp), 1)
                self.assertEqual(list(Path(tmp).rglob('*.verified')), [])
            else:
                root, units = staging.stage('https://example.org/site/', Path(tmp), 1)
                self.assertEqual(units, {('example', '1')})
                self.assertEqual((root / 'plugins/example_1.jar').read_bytes(), payload)
                mocked.reset_mock()
                staging.stage('https://example.org/site/', Path(tmp), 1)
                mocked.assert_not_called()

    def test_corrupt_payload_never_gets_completion_marker(self):
        self.exercise(corrupt=True)

    def test_completed_snapshot_reuses_verified_payload(self):
        self.exercise()


if __name__ == '__main__':
    unittest.main()
