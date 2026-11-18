import hashlib
import zipfile
from pathlib import PurePosixPath


def inspect_bundle(path, manifest):
    entries = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        expected = manifest['files']
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('bundle members disagree with manifest')
        for name in names:
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or '..' in parsed.parts or '\\' in name or ':' in name:
                raise ValueError('unsafe member path')
            info = archive.getinfo(name)
            if info.file_size > 8_000_000 or info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('unsupported bundle member')
            data = archive.read(name)
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected[name]['sha256'] or len(data) != expected[name]['size']:
                raise ValueError('member digest mismatch')
            entries.append({'name': name, 'sha256': digest, 'size': len(data), 'data': data})
    return entries
