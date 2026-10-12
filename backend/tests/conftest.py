"""Suite-wide test defaults for the V-Maker backend.

The persistent task queue is drained by ``backend.worker.TaskWorker``, which starts inside the
application lifespan. Many existing tests drive a task by hand (``POST /api/tasks/{id}/execute``
or ``POST /api/tasks/execute-next``) and assert on the row right after that call, so an
always-on worker racing them would make those assertions time-dependent.

``create_app(path)`` therefore follows ``VMAKER_WORKER_ENABLED`` (default: on, i.e. production
behaviour) and the suite pins it off here. Tests that exercise the worker pass
``create_app(path, start_worker=True)`` or drive ``run_pending_once()`` directly.

To run the whole suite with the worker enabled instead:

    $env:VMAKER_WORKER_ENABLED='1'; python -m pytest -q
"""

from __future__ import annotations

import os

# Set before any test imports ``backend.main``, whose module-level ``app = create_app()``
# reads the environment once.
os.environ.setdefault("VMAKER_WORKER_ENABLED", "0")
