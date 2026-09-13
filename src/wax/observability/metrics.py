"""Metrics — runtime-collected quantitative measurements.

Simple in-memory metrics registry for now. A future Phase S will add
Prometheus export, OpenTelemetry tracing, etc.

Metric types:
- Counter: monotonically increasing (e.g., requests_total)
- Gauge: arbitrary value (e.g., active_executions)
- Histogram: distribution (e.g., llm_latency_ms)

All metrics are tagged with the principal_id where applicable. Aggregated
metrics (without principal_id) are also tracked for system-level visibility.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


@dataclass
class Counter:
    """Monotonically increasing value."""

    name: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def inc(self, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("Counter can only increase")
        self.value += amount


@dataclass
class Gauge:
    """Arbitrary value (can go up or down)."""

    name: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount


@dataclass
class HistogramBucket:
    upper_bound: float
    count: int = 0


@dataclass
class Histogram:
    """Distribution of values.

    Tracks count + sum + bucketed counts. Default buckets are tuned for
    latency measurements (milliseconds).
    """

    name: str
    buckets: list[HistogramBucket] = field(
        default_factory=lambda: [
            HistogramBucket(upper_bound=bound)
            for bound in (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, float("inf"))
        ]
    )
    count: int = 0
    sum: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        for bucket in self.buckets:
            if value <= bucket.upper_bound:
                bucket.count += 1


class MetricsRegistry:
    """In-memory metrics registry.

    Not a full Prometheus client — just enough to expose counters, gauges,
    and histograms from the runtime. A future phase will add a /metrics
    endpoint that exports these in Prometheus format.
    """

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = Lock()

    def counter(self, name: str, **labels: str) -> Counter:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._counters:
                self._counters[key] = Counter(name=name, labels=labels)
            return self._counters[key]

    def gauge(self, name: str, **labels: str) -> Gauge:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._gauges:
                self._gauges[key] = Gauge(name=name, labels=labels)
            return self._gauges[key]

    def histogram(self, name: str, **labels: str) -> Histogram:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram(name=name, labels=labels)
            return self._histograms[key]

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of all metrics.

        Useful for /metrics endpoint or for tests.
        """
        with self._lock:
            return {
                "counters": {
                    k: {"value": c.value, "labels": c.labels}
                    for k, c in self._counters.items()
                },
                "gauges": {
                    k: {"value": g.value, "labels": g.labels}
                    for k, g in self._gauges.items()
                },
                "histograms": {
                    k: {
                        "count": h.count,
                        "sum": h.sum,
                        "buckets": [(b.upper_bound, b.count) for b in h.buckets],
                        "labels": h.labels,
                    }
                    for k, h in self._histograms.items()
                },
            }

    def _key(self, name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}|{label_str}"


# Singleton metrics registry
_global_metrics: MetricsRegistry | None = None
_global_metrics_lock = Lock()


def get_metrics() -> MetricsRegistry:
    """Get the global metrics registry (singleton)."""
    global _global_metrics
    with _global_metrics_lock:
        if _global_metrics is None:
            _global_metrics = MetricsRegistry()
        return _global_metrics
