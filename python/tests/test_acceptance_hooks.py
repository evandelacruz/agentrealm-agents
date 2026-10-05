"""Every acceptance metrics class takes the runner's real hook calls (A36).

A metrics class that overrides a hook with an older signature raises
``TypeError`` only when the runner calls it, which would first show on a live
run. Here the real ``Runner`` sends ticks with each class attached.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading

import agentrealm_agent
from agentrealm_agent.acceptance import AcceptanceHooks
from tests.test_m6_acceptance import FakeMovementServer, RunnerCase

for _mod in pkgutil.iter_modules(agentrealm_agent.__path__):
    if _mod.name.endswith("_acceptance"):
        importlib.import_module(f"agentrealm_agent.{_mod.name}")

# Constructor arguments for metrics classes that have required ones.
ARGS: dict[str, dict] = {
    "M7AcceptanceMetrics": {"overworld_map_id": 7, "origin": (0, 0), "target": (80, 0)},
}


def metrics_classes() -> list[type]:
    """Every concrete subclass of AcceptanceHooks, at any depth."""
    out, todo = [], list(AcceptanceHooks.__subclasses__())
    while todo:
        cls = todo.pop()
        todo.extend(cls.__subclasses__())
        if cls.__module__.endswith("_acceptance"):
            out.append(cls)
    return out


class EveryMetricsClassTest(RunnerCase):
    def test_runner_hooks_reach_each_metrics_class(self):
        classes = metrics_classes()
        self.assertGreaterEqual(len(classes), 5, "the *_acceptance modules were imported")
        for cls in classes:
            with self.subTest(cls.__name__):
                stop = threading.Event()
                metrics = cls(**ARGS.get(cls.__name__, {}))
                server = FakeMovementServer(10, stop)
                r = self.make_runner(server, stop, metrics)
                for _ in range(3):
                    r.tick()  # before_tick, then the response's hooks
                    metrics.on_window(urgent=False, alive=True)
