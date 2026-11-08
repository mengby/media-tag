"""Test-only PID marker; production entry remains media-tag mcp-stdio."""
import os
from pathlib import Path
import sys
from media_tag.cli import main

if __name__=='__main__':
    Path(sys.argv[2]).write_text(str(os.getpid()))
    raise SystemExit(main(['mcp-stdio','--config',sys.argv[1]]))
