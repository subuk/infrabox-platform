"""Runtime compatibility for configure PRs, independent of the source commit."""
import hashlib
from pathlib import Path
import sys
FILES = ('runtime/Containerfile', 'requirements.txt', 'requirements.yml',
         'scripts/runtime_contract.py', 'scripts/configure.py', 'scripts/impact.py',
         'scripts/discover.py', 'scripts/results.py')
def fingerprint(source):
    digest=hashlib.sha256()
    for name in FILES:
        digest.update(name.encode()+b'\0'+(Path(source)/name).read_bytes()+b'\0')
    return digest.hexdigest()
if __name__ == '__main__':
    print(fingerprint(sys.argv[1]))
