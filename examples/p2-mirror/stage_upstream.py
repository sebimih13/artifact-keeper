#!/usr/bin/env python3
"""Stage an Eclipse composite and its payloads locally for fast Ant mirroring.

Supports regular Eclipse bundle/feature/binary mappings. Unsupported mappings
fail explicitly rather than silently producing an incomplete site.
"""
import argparse
import concurrent.futures
import hashlib
import io
import re
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


def get(url):
    for attempt in range(5):
        try:
            return urllib.request.urlopen(url, timeout=180)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
            time.sleep(min(60, max(1, int(error.headers.get('Retry-After', '5')))))


def read_catalog(base, stem):
    for ext in ('.jar', '.xml'):
        try:
            with get(urllib.parse.urljoin(base, stem + ext)) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                continue
            raise
        xml = zipfile.ZipFile(io.BytesIO(data)).read(stem + '.xml') if ext == '.jar' else data
        root = ET.fromstring(xml)
        return root, stem + ext, xml
    return None


def save_catalog(directory, stem, root, source_xml=None):
    instructions = re.findall(rb'<\?(?!xml\s).*?\?>', source_xml or b'')
    xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + b'\n'.join(instructions) + b'\n' + ET.tostring(root, encoding='utf-8')
    with zipfile.ZipFile(directory / (stem + '.jar'), 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(stem + '.xml', xml)


def stage(source, work, workers=12):
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    plan_file = work / 'plan.json'
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
        if plan['source'] != source:
            raise ValueError('Staging plan belongs to another source')
    else:
        jobs = []
        units = set()
        visited = {}
        leaves = []
        def visit(url):
            url = url.rstrip('/') + '/'
            if url in visited:
                return visited[url]
            directory = work / hashlib.sha256(url.encode()).hexdigest()[:20]
            directory.mkdir(exist_ok=True)
            visited[url] = directory
            composite = read_catalog(url, 'compositeContent')
            if composite:
                artifact_composite = read_catalog(url, 'compositeArtifacts')
                if not artifact_composite:
                    raise ValueError('Missing artifact composite: ' + url)
                metadata_children = [urllib.parse.urljoin(url, c.get('location')) for c in composite[0].findall('./children/child')]
                artifact_children = [urllib.parse.urljoin(url, c.get('location')) for c in artifact_composite[0].findall('./children/child')]
                if metadata_children != artifact_children:
                    raise ValueError('Different metadata/artifact children require a custom staging plan: ' + url)
                children = [visit(child).as_uri() + '/' for child in metadata_children]
                for stem, catalog in [('compositeContent', composite), ('compositeArtifacts', artifact_composite)]:
                    root = catalog[0]
                    for child, location in zip(root.findall('./children/child'), children):
                        child.set('location', location)
                    save_catalog(directory, stem, root, catalog[2])
                (directory / 'p2.index').write_text('version=1\nmetadata.repository.factory.order=compositeContent.xml,\\!\nartifact.repository.factory.order=compositeArtifacts.xml,\\!\n')
            else:
                metadata = read_catalog(url, 'content')
                artifacts = read_catalog(url, 'artifacts')
                if not metadata or not artifacts:
                    raise ValueError('Missing regular p2 catalogs: ' + url)
                save_catalog(directory, 'content', metadata[0], metadata[2])
                save_catalog(directory, 'artifacts', artifacts[0], artifacts[2])
                (directory / 'p2.index').write_text('version=1\nmetadata.repository.factory.order=content.xml,\\!\nartifact.repository.factory.order=artifacts.xml,\\!\n')
                units.update((u.get('id'), u.get('version')) for u in metadata[0].findall('./units/unit'))
                mappings = [r.get('output') for r in artifacts[0].findall('./mappings/rule')]
                for artifact in artifacts[0].findall('./artifacts/artifact'):
                    classifier, name, version = (artifact.get(k) for k in ('classifier', 'id', 'version'))
                    folder = {'osgi.bundle': 'plugins', 'org.eclipse.update.feature': 'features', 'binary': 'binary'}[classifier]
                    template = '${repoUrl}/' + folder + '/${id}_${version}' + ('' if folder == 'binary' else '.jar')
                    if template not in mappings or any('/' in value or '\\' in value or value in ('.', '..') for value in (name, version)):
                        raise ValueError('Unsupported or unsafe artifact mapping: ' + str(artifact.attrib))
                    props = {p.get('name'): p.get('value') for p in artifact.findall('./properties/property')}
                    if props.get('format'):
                        raise ValueError('Non-canonical artifact format needs a custom plan: ' + str(artifact.attrib))
                    relative = folder + '/' + name + '_' + version + ('' if folder == 'binary' else '.jar')
                    jobs.append(dict(url=urllib.parse.urljoin(url, relative), path=str(directory / relative),
                                     sha256=props.get('download.checksum.sha-256'),
                                     sha512=props.get('download.checksum.sha-512'),
                                     md5=props.get('download.md5'), size=props.get('download.size')))
                leaves.append(dict(url=url, artifacts=len(artifacts[0].findall('./artifacts/artifact')),
                                   units=len(metadata[0].findall('./units/unit'))))
            return directory
        root = visit(source)
        plan = dict(source=source, root=str(root), jobs=jobs, units=sorted(units), leaves=leaves)
        plan_file.write_text(json.dumps(plan, indent=2) + '\n')
    def valid(path, job):
        if not path.is_file() or (job['size'] and path.stat().st_size != int(job['size'])):
            return False
        for algo in ('sha256', 'sha512', 'md5'):
            if job.get(algo):
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, algo).hexdigest() != job[algo]:
                        return False
        # A completion sidecar records a locally computed checksum even where
        # upstream did not publish one, so interrupted downloads cannot be reused.
        marker = path.with_name(path.name + '.verified')
        if not marker.exists():
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == marker.read_text()
    def download(job):
        path = Path(job['path'])
        if valid(path, job):
            return path.stat().st_size
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.part')
        with get(job['url']) as response, temporary.open('wb') as output:
            while block := response.read(1024*1024):
                output.write(block)
        if job['size'] and temporary.stat().st_size != int(job['size']):
            raise ValueError('Size mismatch: ' + job['url'])
        for algo in ('sha256', 'sha512', 'md5'):
            if job.get(algo):
                with temporary.open('rb') as stream:
                    if hashlib.file_digest(stream, algo).hexdigest() != job[algo]:
                        raise ValueError('Checksum mismatch: ' + job['url'])
        temporary.replace(path)
        with path.open('rb') as stream:
            path.with_name(path.name + '.verified').write_text(hashlib.file_digest(stream, 'sha256').hexdigest())
        return path.stat().st_size
    total = 0
    print('Staging leaves:', plan['leaves'], flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, size in enumerate(pool.map(download, plan['jobs']), 1):
            total += size
            if number % 100 == 0 or number == len(plan['jobs']):
                print('Staged:', number, '/', len(plan['jobs']), 'bytes:', total, flush=True)
    print('Staging complete:', total, 'bytes', flush=True)
    return Path(plan['root']), set(map(tuple, plan['units']))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--workers', type=int, default=12, choices=range(1, 17))
    args = p.parse_args()
    stage(args.source, args.work, args.workers)
