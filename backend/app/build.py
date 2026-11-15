"""Runtime build identity. Restart after changes; package metadata pins this build."""
import hashlib
import subprocess
from .config import BACKEND_DIR, PROJECT_ROOT


def _identity():
    files=sorted((BACKEND_DIR/'app').rglob('*.py'))+[BACKEND_DIR/'app'/'schema.sql']+sorted(p for p in (BACKEND_DIR/'static').rglob('*') if p.is_file() and p.suffix in {'.html','.js','.css'})
    h=hashlib.sha256()
    for path in files:
        h.update(str(path.relative_to(PROJECT_ROOT)).encode());h.update(path.read_bytes())
    try:
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=PROJECT_ROOT,text=True).strip()
    except (OSError,subprocess.CalledProcessError):commit='source-archive'
    return {'commit':commit,'source_sha256':h.hexdigest(),'version':'build-'+h.hexdigest()[:16]}


BUILD=_identity()
