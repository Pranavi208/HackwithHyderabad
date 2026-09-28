"""Loads the synthetic dataset and keeps incident state (suggestions, resolutions, post-mortems) on disk."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .triage import triage


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


EMPTY_STATE = {"resolutions": {}, "suggestions": {}, "postmortems": {}}


class Store:
    """In-process view of services, incidents and the on-call team's work so far."""

    def __init__(self, data_dir: Path, state_path: Path):
        self.state_path = state_path
        self.services: dict[str, dict] = {s["id"]: s for s in _load(data_dir / "services.json")}
        self.incidents: list[dict] = sorted(_load(data_dir / "incidents.json"), key=lambda i: (i["started_at"], i["id"]))
        self.by_id: dict[str, dict] = {i["id"]: i for i in self.incidents}
        self.history: list[dict] = _load(data_dir / "history.json")
        self.ground_truth: dict[str, dict] = _load(data_dir / "ground_truth.json")
        self._triage: dict[str, dict] = {}
        self.state: dict[str, dict] = json.loads(json.dumps(EMPTY_STATE))
        if state_path.exists():
            self.state.update(_load(state_path))

    def triage_for(self, incident_id: str) -> dict[str, Any]:
        """Deterministic triage (cached). Duplicates are checked against earlier incidents."""
        if incident_id not in self._triage:
            inc = self.by_id[incident_id]
            idx = self.incidents.index(inc)
            self._triage[incident_id] = triage(inc, self.services[inc["service_id"]], self.incidents[:idx])
        return self._triage[incident_id]

    def context(self, incident_id: str) -> dict[str, Any]:
        """Incident with its service, triage and state."""
        inc = self.by_id[incident_id]
        return {
            "incident": inc,
            "service": self.services[inc["service_id"]],
            "triage": self.triage_for(incident_id),
            "suggestion": self.state["suggestions"].get(incident_id),
            "resolution": self.state["resolutions"].get(incident_id),
            "postmortem": self.state["postmortems"].get(incident_id),
        }

    def record_suggestion(self, incident_id: str, suggestion: dict) -> None:
        self.state["suggestions"][incident_id] = suggestion
        self.save()

    def record_resolution(self, incident_id: str, resolution: dict) -> None:
        self.state["resolutions"][incident_id] = resolution
        self.save()

    def record_postmortem(self, incident_id: str, text: str) -> None:
        self.state["postmortems"][incident_id] = text
        self.save()

    def reset(self) -> None:
        """Forget all suggestions, resolutions and post-mortems (memory is reset separately)."""
        self.state = json.loads(json.dumps(EMPTY_STATE))
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
