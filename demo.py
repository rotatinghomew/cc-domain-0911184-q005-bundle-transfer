import json
import tempfile
from pathlib import Path
from transfer.fetch import Downloader
from transfer.package import inspect_bundle
from transfer.receiving import Receiving
from transfer.source import serve


def main():
    with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
        root = Path(directory)
        manifest = source.manifest(url)
        package = Downloader().fetch(manifest, root / 'delivery.zip')
        receiving = Receiving(root / 'received')
        receiving.accept(manifest, inspect_bundle(package, manifest))
        print(json.dumps({'asset': manifest['asset'], 'members': receiving.rows()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
