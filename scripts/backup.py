"""Stop bot and worker first. python -m scripts.backup /backups/aurelia.zip"""
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from aurelia_utils.config import Config


def main():
    config=Config.env()
    destination=Path(sys.argv[1]).resolve()
    if config.root == destination or config.root in destination.parents:
        raise SystemExit('Backup path must be outside DATA_DIR')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        with sqlite3.connect(config.root/'aurelia.sqlite3') as source, sqlite3.connect(Path(temp)/'aurelia.sqlite3') as target:
            source.backup(target)
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(Path(temp)/'aurelia.sqlite3','aurelia.sqlite3')
            for path in (config.root/'users').rglob('*'):
                if path.is_file() and path.suffix!='.part':
                    archive.write(path,path.relative_to(config.root))
    print(destination)

if __name__=='__main__': main()
