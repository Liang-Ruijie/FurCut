"""Single service owner per database. A second port must not recover a live worker."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os


@contextmanager
def exclusive_runtime(path: Path):
    lock_path=Path(str(path.resolve())+'.service-lock')
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('同一数据库已有服务运行；禁止第二实例恢复或重发任务，请使用数据库副本') from exc
        handle.seek(0);handle.truncate();handle.write(str(os.getpid()));handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
