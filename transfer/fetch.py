import hashlib
from pathlib import Path
from urllib.request import urlopen


class Downloader:
    def fetch(self, manifest, destination):
        destination = Path(destination)
        temporary = destination.with_suffix(destination.suffix + '.partial')
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(manifest['url'], timeout=5) as response, temporary.open('wb') as output:
                if response.headers.get_content_type() not in ('application/zip', 'application/octet-stream'):
                    raise ValueError('unexpected download content type')
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
                    output.write(block)
            if size != manifest['size'] or digest.hexdigest() != manifest['sha256']:
                raise ValueError('bundle size or digest mismatch')
            temporary.replace(destination)
            return destination
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
