"""Offline checks for corrupt input and publication boundaries."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

spec = importlib.util.spec_from_file_location('seed_local', Path(__file__).with_name('seed-local.py'))
seed_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_local)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.site = self.work / 'site'
        (self.site / 'plugins').mkdir(parents=True)
        self.payload = b'example artifact bytes'
        (self.site / 'plugins/example_1.0.0.jar').write_bytes(self.payload)
        digest = hashlib.sha256(self.payload).hexdigest()
        xml = f'''<repository type="SimpleArtifactRepository"><artifacts>
        <artifact classifier="osgi.bundle" id="example" version="1.0.0"><properties>
        <property name="download.checksum.sha-256" value="{digest}"/>
        </properties></artifact></artifacts></repository>'''
        content = '''<repository type="LocalMetadataRepository"><units>
        <unit id="example" version="1.0.0"><artifacts>
        <artifact classifier="osgi.bundle" id="example" version="1.0.0"/>
        </artifacts></unit></units></repository>'''
        for name, text in [('artifacts', xml), ('content', content)]:
            with zipfile.ZipFile(self.site / (name + '.jar'), 'w') as jar:
                jar.writestr(name + '.xml', text)
        self.args = argparse.Namespace(work=self.work, repository='test', source='https://example.org/',
                                       workers=1, publish=True)

    def test_checks_payload_not_only_metadata(self):
        result = seed_local.inventory(self.site)
        self.assertEqual(result['source_checksum_verified'], 1)
        (self.site / 'plugins/example_1.0.0.jar').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'sha256 mismatch'):
            seed_local.inventory(self.site)

    def test_missing_payload_fails_validation(self):
        (self.site / 'plugins/example_1.0.0.jar').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            seed_local.inventory(self.site)

    def test_does_not_adopt_existing_repository(self):
        report = seed_local.inventory(self.site)
        client = Mock()
        client.request.return_value = {'id': 'someone-elses-repo'}
        with patch.object(seed_local, 'Client', return_value=client):
            with self.assertRaisesRegex(ValueError, 'not created by this run'):
                seed_local.seed(self.args, report)
        self.assertEqual(client.request.call_args_list[0].args, ('GET', '/api/v1/repositories/test'))
        self.assertEqual(client.request.call_count, 1)

    def test_corrupt_download_never_publishes(self):
        report = seed_local.inventory(self.site)
        (self.work / 'repository.json').write_text(json.dumps(dict(base='https://localhost', key='test', id='owned')))
        client = Mock()
        client.base = 'https://localhost'
        client.hosted_files.return_value = {f['path']: f['sha256'] for f in report['files']}
        def request(method, path, **kwargs):
            if method == 'GET' and path == '/api/v1/repositories/test':
                return dict(id='owned', format='p2', repo_type='local', is_public=False)
            if method == 'GET' and path.startswith('/general/'):
                return b'corrupt download'
            self.fail('Unexpected request: ' + method + ' ' + path)
        client.request.side_effect = request
        with patch.object(seed_local, 'Client', return_value=client):
            with self.assertRaisesRegex(ValueError, 'Download checksum mismatch'):
                seed_local.seed(self.args, report)
        self.assertFalse(any(call.args[0] == 'PATCH' for call in client.request.call_args_list))
        self.assertFalse((self.work / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
