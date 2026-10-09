from pathlib import Path

from app.services.job_store import JobStore


def test_job_store_persists_between_instances(monkeypatch):
    database = Path.cwd() / "test-jobs.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            path.unlink()
    try:
        first = JobStore(database)
        first["job-1"] = {"status": "processing", "progress": 40}

        second = JobStore(database)
        assert second["job-1"] == {"status": "processing", "progress": 40}

        second["job-1"] = {"status": "complete", "progress": 100}
        assert first.get("job-1")["status"] == "complete"
        assert "job-1" in first
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.exists():
                path.unlink()


def test_recover_incomplete_marks_only_processing_jobs():
    database = Path.cwd() / "test-recovery-jobs.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            path.unlink()
    try:
        store = JobStore(database)
        store["running"] = {"status": "processing", "stage": "rendering", "progress": 65}
        store["done"] = {"status": "complete", "progress": 100}

        assert store.recover_incomplete() == 1
        assert store["running"]["status"] == "failed"
        assert store["running"]["stage"] == "interrupted"
        assert store["done"]["status"] == "complete"
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.exists():
                path.unlink()
