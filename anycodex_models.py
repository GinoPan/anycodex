"""Merge vendor metadata into a current Codex model list without pinning it."""
import copy


def merge_model_catalog(data, vendors):
    """Return a merged copy; preserve official entries and response metadata.

    Clone the current official schema instead of maintaining a version-specific
    boilerplate. Only explicitly configured vendor slugs are upserted. Repeated
    merges are idempotent, including when the input is the local picker cache.
    """
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("expected a Codex model catalog with a models array")
    wanted = {m["slug"]: m for v in vendors for m in v["models"]}
    result = copy.deepcopy(data)
    if not wanted:
        return result
    models = result["models"]
    prefixes = tuple(p for v in vendors for p in v["match_prefixes"])
    template = next((m for m in models
                     if m.get("visibility") == "list"
                     and not m.get("slug", "").startswith(prefixes)), None)
    if template is None:
        raise ValueError("no listed official model available as metadata template")

    existing = {m.get("slug"): i for i, m in enumerate(models)}
    priority = max((m.get("priority") or 0) for m in models) + 1
    for slug, spec in wanted.items():
        index = existing.get(slug)
        entry = copy.deepcopy(template if index is None else models[index])
        entry.update(copy.deepcopy(spec))
        entry.update({
            "priority": priority if index is None else entry.get("priority", priority),
            "max_context_window": spec["context_window"],
            "visibility": "list",
            "supported_in_api": True,
            "prefer_websockets": False,
            "is_default": False,
            "upgrade": None,
            "availability_nux": None,
            "base_instructions": "",
            "additional_speed_tiers": [],
            "service_tiers": [],
            "default_service_tier": None,
            "available_access_programs": None,
            "multi_agent_version": None,
            "model_messages": None,
            "use_responses_lite": False,
            "supports_search_tool": False,
        })
        if index is None:
            models.append(entry)
            priority += 1
        else:
            models[index] = entry
    return result
