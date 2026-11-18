"""Local, offline demonstration of resumable acquisition and delta delivery.

Run ``python3 demo.py``.  Everything happens against a loopback source in a
temporary directory; nothing leaves the machine.
"""

import json
import tempfile
from pathlib import Path

from transfer.acquire import Acquisition
from transfer.client import ManifestClient
from transfer.fetch import Downloader
from transfer.receiving import Receiving
from transfer.source import serve


def main():
    with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
        root = Path(directory)
        receiving = Receiving(root / 'received')
        acquisition = Acquisition(receiving, client=ManifestClient(url),
                                  workdir=root / 'work',
                                  downloader=Downloader(attempts=4))

        # 1. A normal full delivery.
        first = acquisition.collect()
        print(json.dumps({'step': 'full', 'receipt': first}, ensure_ascii=False))

        # 2. A delta that replaces one subtitle and removes the cover.  The
        #    source only ships the changed file; the final set is built from
        #    the exact version 1 already in the receiving list.
        main_srt = source.files['subtitles/main.srt']
        source.publish(version=2, token='signed-v2', mode='delta', base_version=1,
                       files={'subtitles/main.srt': main_srt.replace(b'1\n', b'2\n')},
                       removals=['cover.txt'])
        source.final_files = {'subtitles/main.srt': source.files['subtitles/main.srt']}
        second = acquisition.collect(version=2)
        print(json.dumps({'step': 'delta', 'receipt': second,
                          'versions': receiving.versions('delivery-a')},
                         ensure_ascii=False))

        # 3. An interrupted transfer leaves unverified bytes; a fresh signed
        #    URL for the same version lets the next run continue from the
        #    bytes the source confirms.
        source.final_files = None
        large = b'3\ninterrupted delivery\n' * 8000
        source.publish(version=3, token='signed-v3',
                       files={'subtitles/main.srt': large})
        manifest = source.manifest(url)
        partial_target = root / 'v3.zip'
        source.fail_after = manifest['size'] // 2
        try:
            Downloader(attempts=1).fetch(manifest, partial_target)
        except Exception as error:  # expected interruption
            print(json.dumps({'step': 'interrupted', 'reason': type(error).__name__}))
        source.token = 'signed-v3b'
        # The signed address changed; the content did not.
        Downloader().fetch(source.manifest(url), partial_target)
        print(json.dumps({'step': 'resumed',
                          'range_requests': [entry['range'] for entry in source.requests
                                             if entry['path'].startswith('/download/')]},
                         ensure_ascii=False))

        print(json.dumps({'rows': receiving.rows(),
                          'diagnosis': receiving.diagnose('delivery-a')},
                         ensure_ascii=False))


if __name__ == '__main__':
    main()
