#!/usr/bin/python3
"""Validate a drop-in together with the complete current daemon configuration."""
from pathlib import Path
import subprocess
import sys
import tempfile
candidate=Path(sys.argv[1]).read_text()
# First-value-wins: candidate represents 00-infrabox.conf before existing drop-ins.
# The real full configuration is tested again by the handler before reload.
with tempfile.NamedTemporaryFile(mode='w',prefix='infrabox-sshd-') as stream:
    stream.write(candidate+'\n'+Path('/etc/ssh/sshd_config').read_text())
    stream.flush()
    subprocess.run(['/usr/sbin/sshd','-t','-f',stream.name],check=True)
