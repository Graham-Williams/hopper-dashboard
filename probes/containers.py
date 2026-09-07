"""box-containers: parse ``docker ps --format '{{.Names}} {{.Status}}'`` into a heartbeat.

Runs on the BOX (host, user in the docker group) from deploy/box/containers_probe.sh via a
systemd timer. The dashboard container has no docker socket; this is the only docker reader.
Python 3.9+ stdlib only (the box has 3.12).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ContainerSummary:
    running: List[str] = field(default_factory=list)      # Status starts with "Up"
    unhealthy: List[str] = field(default_factory=list)    # Status mentions "unhealthy"
    not_running: List[str] = field(default_factory=list)  # anything else (Exited, Restarting, Created…)
    restarting: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.running) + len(self.not_running)

    def metrics(self) -> Dict[str, object]:
        return {
            "total": self.total,
            "running_count": len(self.running),
            "unhealthy_count": len(self.unhealthy),
            "not_running_count": len(self.not_running),
            "running": ",".join(sorted(self.running)),
            "unhealthy": ",".join(sorted(self.unhealthy)),
            "not_running": ",".join(sorted(self.not_running)),
            "restarting": ",".join(sorted(self.restarting)),
        }


def parse_docker_ps(text: str) -> ContainerSummary:
    """Each line: ``<name> <status words…>`` e.g. ``jjho-fan-almanac Up 5 weeks (healthy)``.
    Blank lines are ignored; a line with no status is treated as not running."""
    s = ContainerSummary()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        name = parts[0]
        status = parts[1] if len(parts) > 1 else ""
        if status.startswith("Up"):
            s.running.append(name)
        else:
            s.not_running.append(name)
            if status.startswith("Restarting"):
                s.restarting.append(name)
        if "unhealthy" in status.lower():
            s.unhealthy.append(name)
    return s


def summary_note(s: ContainerSummary) -> str:
    bits = ["%d running" % len(s.running)]
    if s.unhealthy:
        bits.append("unhealthy: " + ",".join(sorted(s.unhealthy)))
    if s.not_running:
        bits.append("not running: " + ",".join(sorted(s.not_running)))
    return "; ".join(bits)
