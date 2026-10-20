#!/usr/bin/env python3
"""Generate composite p2 discovery/catalog files locally; never publish them."""
import argparse
import io
import time
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET


def generate(output: Path, name: str, children: list[str], timestamp: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for suffix, kind, repository_class in (
        ('Content', 'metadata', 'CompositeMetadataRepository'),
        ('Artifacts', 'artifact', 'CompositeArtifactRepository'),
    ):
        root = ET.Element('repository', {
            'name': name,
            'type': f'org.eclipse.equinox.internal.p2.{kind}.repository.{repository_class}',
            'version': '1.0.0',
        })
        props = ET.SubElement(root, 'properties', size='2')
        ET.SubElement(props, 'property', name='p2.timestamp', value=str(timestamp))
        ET.SubElement(props, 'property', name='p2.atomic.composite.loading', value='true')
        child_list = ET.SubElement(root, 'children', size=str(len(children)))
        for child in children:
            ET.SubElement(child_list, 'child', location=child)
        ET.indent(root, space='  ')
        instruction = 'compositeMetadataRepository' if suffix == 'Content' else 'compositeArtifactRepository'
        xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<?{instruction} version="1.0.0"?>\n').encode() + ET.tostring(root, encoding='utf-8') + b'\n'
        filename = f'composite{suffix}.xml'
        (output / filename).write_bytes(xml)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as jar:
            # Fixed ZIP date makes repeat generation reproducible with --timestamp.
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            jar.writestr(info, xml)
        (output / f'composite{suffix}.jar').write_bytes(archive.getvalue())
    (output / 'p2.index').write_text(
        'version=1\n'
        'metadata.repository.factory.order=compositeContent.xml,\\!\n'
        'artifact.repository.factory.order=compositeArtifacts.xml,\\!\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--child', action='append', required=True,
                        help='Child update-site URL or relative path; repeat in priority order.')
    parser.add_argument('--timestamp', type=int, default=None, help='p2 timestamp in milliseconds.')
    args = parser.parse_args()
    generate(args.output, args.name, args.child,
             args.timestamp if args.timestamp is not None else int(time.time() * 1000))
    print(f'Generated five composite files in {args.output}')


if __name__ == '__main__':
    main()
