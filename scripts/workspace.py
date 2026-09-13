"""Prepare/clean only this workflow's result directory and abandoned private files."""
import os
from pathlib import Path
import re
import shutil
import sys


def workspace(cleanup=False):
    root = Path(os.environ['RUNNER_TEMP']).resolve()
    path = Path(os.environ['PLATFORM_RESULTS_DIR'])
    if path.is_symlink() or path.parent.resolve() != root or not re.fullmatch(r'discovery-[0-9]+-[0-9]*', path.name):
        raise ValueError('Invalid result workspace')
    if path.exists():
        shutil.rmtree(path)
    if not cleanup:
        private = Path('/run/platform-jobs')
        # Capacity one; preceding jobs have ended before this preparation step.
        for item in private.glob('credentials-*'):
            if item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        if Path('/opt/platform/REVISION').read_text().strip() != os.environ['GITEA_SHA']:
            raise ValueError('Runtime revision does not match the queued workflow')


if __name__ == '__main__':
    workspace('--cleanup' in sys.argv)
