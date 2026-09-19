import os
from pathlib import Path
import shutil
root=Path(os.environ['RUNNER_TEMP']).resolve()
path=Path(os.environ['PLATFORM_CONFIGURE_RESULTS_DIR']).resolve()
if path.parent != root or not path.name.startswith('configure-'):
    raise SystemExit('Invalid configure workspace')
shutil.rmtree(path,ignore_errors=True)
