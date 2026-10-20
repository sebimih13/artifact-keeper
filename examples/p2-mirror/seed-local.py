#!/usr/bin/env python3
"""Mirror an Eclipse update site, seed a local p2 repository, and verify downloads.

Uses the standard library and Eclipse Ant. API_TOKEN is read from the environment.
Run --phase mirror without credentials; --phase seed reuses a completed mirror.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def xml_jar(path, name):
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read(name))


def inventory(site):
    """Check every canonical artifact's file, size and advertised checksums."""
    metadata = xml_jar(site / 'content.jar', 'content.xml')
    artifacts = xml_jar(site / 'artifacts.jar', 'artifacts.xml')
    if 'Composite' in metadata.get('type', '') or 'Composite' in artifacts.get('type', ''):
        raise ValueError('Expected a flat Eclipse mirror')
    keys = set()
    allowed = {'content.jar', 'artifacts.jar', 'p2.index'}
    counts = {}
    checked = 0
    unchecked = 0
    for artifact in artifacts.findall('./artifacts/artifact'):
        classifier, name, version = (artifact.get(k) for k in ('classifier', 'id', 'version'))
        key = (classifier, name, version)
        if key in keys:
            raise ValueError(f'Duplicate artifact descriptor: {key}')
        keys.add(key)
        folder = {'osgi.bundle': 'plugins', 'org.eclipse.update.feature': 'features', 'binary': 'binary'}[classifier]
        path = site / folder / (f'{name}_{version}' + ('' if folder == 'binary' else '.jar'))
        allowed.add(str(path.relative_to(site)))
        props = {p.get('name'): p.get('value') for p in artifact.findall('./properties/property')}
        if not path.is_file():
            raise ValueError(f'Missing artifact: {path}')
        if props.get('download.size') and path.stat().st_size != int(props['download.size']):
            raise ValueError(f'Wrong size: {path}')
        hashes = [(algo, props.get(prop)) for algo, prop in (
            ('sha256', 'download.checksum.sha-256'), ('sha512', 'download.checksum.sha-512'),
            ('md5', 'download.md5')) if props.get(prop)]
        for algo, expected in hashes:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, algo).hexdigest()
            if actual != expected:
                raise ValueError(f'{algo} mismatch: {path}')
        checked += bool(hashes)
        unchecked += not hashes
        counts[folder] = counts.get(folder, 0) + 1
    extra = {str(p.relative_to(site)) for p in site.rglob('*') if p.is_file()} - allowed
    if extra:
        raise ValueError(f'Unexpected files in upload site: {sorted(extra)}')
    units = metadata.findall('./units/unit')
    for unit in units:
        for artifact in unit.findall('./artifacts/artifact'):
            key = tuple(artifact.get(k) for k in ('classifier', 'id', 'version'))
            if key not in keys:
                raise ValueError(f'Missing descriptor required by {unit.get("id")}: {key}')
    (site / 'p2.index').write_text('version=1\nmetadata.repository.factory.order=content.xml,\\!\nartifact.repository.factory.order=artifacts.xml,\\!\n')
    files = [{'path': str(p.relative_to(site)), 'size': p.stat().st_size, 'sha256': digest(p)}
             for p in sorted(site.rglob('*')) if p.is_file()]
    return dict(installable_units=len(units), artifacts=len(keys), counts=counts,
                source_checksum_verified=checked, without_source_checksum=unchecked,
                bytes=sum(f['size'] for f in files), files=files)


class Client:
    def __init__(self, args):
        self.base = args.url.rstrip('/')
        self.token = os.environ.get('API_TOKEN', '')
        if not self.token:
            raise ValueError('Set API_TOKEN for the seed phase')
        self.context = ssl.create_default_context(cafile=str(args.ca) if args.ca else None)

    def request(self, method, path, data=None, raw=False, anonymous=False, headers=None):
        hdr = {} if anonymous else {'Authorization': 'Bearer ' + self.token}
        hdr.update(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            hdr['Content-Type'] = 'application/json'
        # Only retry explicit rate-limit responses: an ambiguous failed mutation
        # must not be repeated without checking server state.
        for attempt in range(8):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdr)
            try:
                with urllib.request.urlopen(req, context=self.context, timeout=180) as response:
                    body = response.read()
                return body if raw else (json.loads(body) if body else None)
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 7:
                    delay = min(300, max(1, int(error.headers.get('Retry-After', '10'))))
                    print(f'Rate limited; retrying in {delay}s', flush=True)
                    time.sleep(delay)
                    continue
                raise

    def hosted_files(self, repo):
        found = {}
        page = 1
        while True:
            result = self.request('GET', f'/api/v1/repositories/{repo}/artifacts?per_page=100&count=exact&page={page}')
            for item in result['items']:
                found[item['path']] = item['checksum_sha256'].strip()
            if page >= result['pagination']['total_pages']:
                return found
            page += 1


def mirror(args):
    expected_units = None
    actual_source = args.source
    if args.stage_upstream:
        from stage_upstream import stage
        staged, expected_units = stage(args.source, args.work / 'upstream')
        actual_source = staged.as_uri() + '/'
    marker = args.work / 'mirror-complete.json'
    if marker.exists():
        if json.loads(marker.read_text())['source'] != args.source:
            raise ValueError('Work directory belongs to a different source')
        print('Using completed mirror; checking all files again', flush=True)
    else:
        config = args.work / 'configuration'
        if not config.exists():
            shutil.copytree(args.eclipse / 'configuration', config,
                            ignore=shutil.ignore_patterns('org.eclipse.osgi', '*.log'))
            p = config / 'config.ini'
            lines = [s for s in p.read_text().splitlines() if not s.startswith(
                ('eclipse.p2.data.area=', 'osgi.configuration.cascaded='))]
            lines += ['eclipse.p2.data.area=' + (args.work / 'p2-data').as_uri() + '/',
                      'osgi.configuration.cascaded=false']
            p.write_text('\n'.join(lines) + '\n')
        launchers = list((args.eclipse / 'plugins').glob('org.eclipse.equinox.launcher_*.jar'))
        if len(launchers) != 1:
            raise ValueError(f'Expected one launcher, found {len(launchers)}')
        command = [str(args.java)]
        if args.truststore:
            command += ['-Djavax.net.ssl.trustStore=' + str(args.truststore),
                        '-Djavax.net.ssl.trustStorePassword=changeit']
        command += ['-Declipse.p2.mirrors=false',
                    '-Dorg.eclipse.ecf.provider.filetransfer.excludeContributors=org.eclipse.ecf.provider.filetransfer.httpclientjava',
                    '-jar', str(launchers[0]), '-configuration', str(config),
                    '-data', str(args.work / 'workspace'), '-nosplash', '-consolelog',
                    '-application', 'org.eclipse.ant.core.antRunner', '-buildfile',
                    str(Path(__file__).with_name('build.xml')), '-Dsource=' + actual_source,
                    '-Ddestination=' + str(args.work / 'site')]
        print('Mirroring with Eclipse Ant; log:', args.work / 'mirror.log', flush=True)
        with (args.work / 'mirror.log').open('a') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    report = inventory(args.work / 'site')
    if expected_units is not None:
        root = xml_jar(args.work / 'site/content.jar', 'content.xml')
        actual_units = {(u.get('id'), u.get('version')) for u in root.findall('./units/unit')}
        if actual_units != expected_units:
            raise ValueError(f'Mirror IU inventory differs: missing={len(expected_units-actual_units)}, extra={len(actual_units-expected_units)}')
        report['all_source_units_present'] = True
    write_json(args.work / 'inventory.json', report)
    write_json(marker, {'source': args.source, 'validated': True, 'completed_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    print('Local validation:', {k: v for k, v in report.items() if k != 'files'}, flush=True)
    return report


def seed(args, report):
    client = Client(args)
    repo = urllib.parse.quote(args.repository, safe='')
    endpoint = '/api/v1/repositories/' + repo
    # Resume only a repository previously created by this run. Never adopt or
    # change visibility of an unrelated pre-existing repository.
    receipt = args.work / 'repository.json'
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous['base'] != client.base or previous['key'] != args.repository:
            raise ValueError('This work directory was seeded into a different target')
        remote = client.request('GET', endpoint)
        if remote['id'] != previous['id'] or remote['format'] != 'p2' or remote['repo_type'] != 'local':
            raise ValueError('Target repository identity changed')
    else:
        try:
            client.request('GET', endpoint)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise ValueError('Repository exists and was not created by this run; choose a fresh key')
        remote = client.request('POST', '/api/v1/repositories', dict(
            key=args.repository, name=args.repository, format='p2', repo_type='local', is_public=False,
            description='Local mirror of ' + args.source))
        write_json(receipt, dict(base=client.base, key=args.repository, id=remote['id']))
    if remote['is_public']:
        # A completed rerun is verification-only, never a public partial update.
        existing = client.hosted_files(repo)
        if any(existing.get(f['path']) != f['sha256'] for f in report['files']):
            raise ValueError('Public target differs from this mirror; use a fresh repository')
    existing = client.hosted_files(repo)
    files = sorted(report['files'], key=lambda f: (
        3 if f['path'] == 'p2.index' else 2 if f['path'].startswith('content.') else
        1 if f['path'].startswith('artifacts.') else 0, f['path']))
    def upload(item):
        path = item['path']
        if path in existing:
            if existing[path] != item['sha256']:
                raise ValueError(f'Refusing to overwrite different bytes: {path}')
            return 'skipped'
        content = (args.work / 'site' / path).read_bytes()
        spec = dict(repository_key=args.repository, artifact_path=path,
                    total_size=len(content), checksum_sha256=item['sha256'], chunk_size=8*1024*1024,
                    content_type='application/java-archive' if path.endswith('.jar') else 'application/octet-stream')
        p = Path(path)
        if p.parent.name in ('plugins', 'features') and p.suffix == '.jar':
            spec['artifact_name'], spec['artifact_version'] = p.stem.rsplit('_', 1)
        session = client.request('POST', '/api/v1/uploads', spec)
        target = '/api/v1/uploads/' + session['session_id']
        step = session['chunk_size']
        for start in range(0, len(content), step):
            chunk = content[start:start+step]
            client.request('PATCH', target, chunk, headers={
                'Content-Type': 'application/octet-stream',
                'Content-Range': f'bytes {start}-{start+len(chunk)-1}/{len(content)}'})
        result = client.request('PUT', target + '/complete')
        if result['checksum_sha256'].strip() != item['sha256']:
            raise ValueError('Server checksum mismatch: ' + path)
        return 'uploaded'
    totals = {'uploaded': 0, 'skipped': 0}
    # Payloads finish before any metadata is published. A small worker pool
    # bounds memory usage to a few artifact files and avoids sequential overhead.
    for stage in (0, 1, 2, 3):
        batch = [f for f in files if (3 if f['path'] == 'p2.index' else 2 if f['path'].startswith('content.') else 1 if f['path'].startswith('artifacts.') else 0) == stage]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            try:
                for number, result in enumerate(pool.map(upload, batch), 1):
                    totals[result] += 1
                    if number % 100 == 0 or number == len(batch):
                        print('Upload stage', stage, number, '/', len(batch), totals, flush=True)
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                raise
    hosted = client.hosted_files(repo)
    if set(hosted) != {f['path'] for f in files} or any(hosted[f['path']] != f['sha256'] for f in files):
        raise ValueError('Hosted inventory differs from the mirrored site')
    def verify(item):
        path = '/general/' + repo + '/' + urllib.parse.quote(item['path'], safe='/')
        body = client.request('GET', path, raw=True)
        if len(body) != item['size'] or hashlib.sha256(body).hexdigest() != item['sha256']:
            raise ValueError('Download checksum mismatch: ' + item['path'])
        return len(body)
    total_bytes = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for number, size in enumerate(pool.map(verify, files), 1):
            total_bytes += size
            if number % 100 == 0 or number == len(files):
                print('Verified downloads:', number, '/', len(files), flush=True)
    result = dict(repository=args.repository, source=args.source, files=len(files), bytes=total_bytes,
                  uploads=totals, installable_units=report['installable_units'], counts=report['counts'],
                  all_download_checksums_match=True, update_site=client.base + '/general/' + repo + '/',
                  public=remote['is_public'])
    if args.publish:
        client.request('PATCH', endpoint, {'is_public': True})
        for filename in ('p2.index', 'content.jar', 'artifacts.jar'):
            body = client.request('GET', '/general/' + repo + '/' + filename, raw=True, anonymous=True)
            if hashlib.sha256(body).hexdigest() != digest(args.work / 'site' / filename):
                raise ValueError('Anonymous metadata verification failed')
        result['public'] = True
    write_json(args.work / 'result.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', default='https://download.eclipse.org/releases/2026-12/')
    p.add_argument('--repository', default='p2-eclipse-2026-12-local')
    p.add_argument('--url', default='https://localhost')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--eclipse', type=Path, default=Path('/home/seb/eclipse/committers-2026-09/eclipse'))
    p.add_argument('--java', type=Path, default=Path('/usr/lib/jvm/java-25-openjdk/bin/java'))
    p.add_argument('--ca', type=Path, default=ROOT / '.local/p2-tls/caddy-root.crt')
    p.add_argument('--truststore', type=Path, default=ROOT / '.local/p2-tls/cacerts')
    p.add_argument('--phase', choices=('all', 'mirror', 'seed'), default='all')
    p.add_argument('--workers', type=int, default=4, choices=range(1, 9))
    p.add_argument('--stage-upstream', action='store_true', help='Stage upstream files in parallel before Ant mirrors them')
    p.add_argument('--publish', action='store_true', help='Enable anonymous reads after full verification')
    args = p.parse_args()
    args.work = args.work.resolve()
    args.eclipse = args.eclipse.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.phase == 'seed' and not (args.work / 'mirror-complete.json').exists():
        p.error('Seed phase requires a successful mirror in the same work directory')
    if args.phase == 'seed':
        completed = json.loads((args.work / 'mirror-complete.json').read_text())
        if completed['source'] != args.source:
            p.error('Source differs from the completed mirror')
        if not completed.get('validated'):
            p.error('Run the mirror phase to validate this checkpoint before seeding')
    report = mirror(args) if args.phase != 'seed' else inventory(args.work / 'site')
    if args.phase != 'mirror':
        seed(args, report)


if __name__ == '__main__':
    main()
