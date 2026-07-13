"""
Turns Home Assistant entity state into widget payloads, per entities.yaml.

Kept separate from publish.py (the HA connection/event loop) so the mapping
logic can be unit tested without a live HA connection.
"""

from __future__ import annotations

from typing import Any


def _get_attr_path(state_obj: dict[str, Any], path: str) -> Any:
    """path is 'state' or 'attributes.temperature' etc."""
    if path == "state":
        return state_obj.get("state")
    value: Any = state_obj
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def resolve_field(spec: dict[str, Any], entity_states: dict[str, dict[str, Any]]) -> Any:
    if "static" in spec:
        return spec["static"]

    entity_id = spec.get("entity")
    if not entity_id:
        return None
    state_obj = entity_states.get(entity_id)
    if state_obj is None:
        return None

    value = _get_attr_path(state_obj, spec.get("attribute", "state"))
    if value is None:
        return None

    mapping = spec.get("map")
    if mapping and isinstance(value, str) and value in mapping:
        value = mapping[value]

    if "round" in spec:
        try:
            digits = int(spec["round"])
            rounded = round(float(value), digits)
            value = int(rounded) if digits == 0 else rounded
        except (TypeError, ValueError):
            pass  # leave value as-is if it isn't numeric (e.g. "unavailable")

    return value


def referenced_entities(node: Any) -> set[str]:
    """Walk a widget config sub-tree and collect every `entity:` id it
    depends on, so publish.py knows which widgets to recompute when a
    specific entity changes."""
    ids: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "entity" in obj:
                ids.add(obj["entity"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(node)
    return ids


def build_widget_payload(
    widget_cfg: dict[str, Any], entity_states: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    wtype = widget_cfg.get("type")

    if wtype == "text_list":
        items = []
        for item_cfg in widget_cfg.get("items", []):
            entry = {
                "label": resolve_field(item_cfg.get("label", {}), entity_states),
                "value": resolve_field(item_cfg.get("value", {}), entity_states),
            }
            if "color" in item_cfg:
                entry["color"] = resolve_field(item_cfg["color"], entity_states)
            items.append(entry)
        payload: dict[str, Any] = {"items": items}
        if "title" in widget_cfg:
            payload["title"] = widget_cfg["title"]
        return payload

    if wtype == "calendar":
        events = []
        for ev_cfg in widget_cfg.get("events", []):
            events.append(
                {
                    "time": resolve_field(ev_cfg.get("time", {}), entity_states),
                    "title": resolve_field(ev_cfg.get("title", {}), entity_states),
                }
            )
        return {"events": events}

    if wtype == "alert_banner":
        active = False
        cond = widget_cfg.get("active_if")
        if cond:
            state_obj = entity_states.get(cond.get("entity", ""))
            if state_obj is not None:
                value = _get_attr_path(state_obj, cond.get("attribute", "state"))
                active = str(value) == str(cond.get("equals"))
        payload = {"active": active}
        for key, spec in widget_cfg.get("fields", {}).items():
            payload[key] = resolve_field(spec, entity_states)
        return payload

    # Generic case (metric, weather, progress, header, ...): flat field map.
    payload = {}
    for key, spec in widget_cfg.get("fields", {}).items():
        payload[key] = resolve_field(spec, entity_states)
    return payload
