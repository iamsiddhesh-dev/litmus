"""Worker package: run lifecycle, verification, evidence, queue entry point.

``WorkerSettings`` is re-exported here so the documented dev command
(``arq litmus.worker.WorkerSettings``) resolves.
"""

from __future__ import annotations

from litmus.worker.settings import WorkerSettings, run_lifecycle_job

__all__ = ["WorkerSettings", "run_lifecycle_job"]