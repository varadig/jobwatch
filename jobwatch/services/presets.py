"""Presets: named searches (terms, filters, scoring) to switch between."""
import copy
import json
import os
import re

from jobwatch.config import migrate_scoring, migrate_search
from jobwatch.env import TEMPLATE_DIR

# A preset is a named "what am I looking for": the search and the scoring. Switching re-scores every posting.
PRESET_KEYS = ("search_terms", "search_locations", "search_filter", "scoring", "min_score")


_TEMPLATE_CACHE = {}


def template_presets():
    """{name: preset} from templates/*.json (the job-type templates, read-only). Cached until a file changes."""
    names = sorted(os.listdir(TEMPLATE_DIR)) if os.path.isdir(TEMPLATE_DIR) else []
    stamp = tuple((fn, os.path.getmtime(os.path.join(TEMPLATE_DIR, fn))) for fn in names)
    if _TEMPLATE_CACHE.get("stamp") != stamp:
        _TEMPLATE_CACHE.update(stamp=stamp, value=_read_templates(names))
    return copy.deepcopy(_TEMPLATE_CACHE["value"])


def _read_templates(names):
    out = {}
    for fn in names:
        if not fn.endswith(".json") or fn.startswith("profile"):
            continue
        try:
            with open(os.path.join(TEMPLATE_DIR, fn), encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, ValueError):
            continue
        if "search_terms" not in t and "linkedin_searches" not in t:
            continue
        t = dict(t)
        migrate_search(t)
        migrate_scoring(t, t)
        out[fn[:-5]] = {k: copy.deepcopy(t[k]) for k in PRESET_KEYS if k in t}
    return out


def preset_of(cfg):
    return {k: copy.deepcopy(cfg.get(k)) for k in PRESET_KEYS if k in cfg}


def apply_preset(cfg, preset):
    for k in PRESET_KEYS:
        if k in preset:
            cfg[k] = copy.deepcopy(preset[k])


NEW_PRESET = "My search"


def preset_by_key(cfg, key):
    """The preset behind a Presets-box value: one of yours, or "tpl:<template>"."""
    if (key or "").startswith("tpl:"):
        return template_presets().get(key[4:])
    return (cfg.get("presets") or {}).get(key)


def same_as_preset(cfg, preset):
    """True if the current search + scoring equal the preset (the keys the preset has)."""
    if not preset:
        return False

    def eq(a, b):  # a missing list / flag counts as an empty one
        if not a and not b:
            return True
        if isinstance(a, dict) or isinstance(b, dict):
            a, b = a or {}, b or {}
            return isinstance(a, dict) and isinstance(b, dict) and all(eq(a.get(k), b.get(k)) for k in set(a) | set(b))
        return a == b
    return all(eq(cfg.get(k), v) for k, v in preset.items() if k in PRESET_KEYS)


def free_preset_name(cfg, base=NEW_PRESET):
    have = set(cfg.get("presets") or {})
    if base not in have:
        return base
    return next("%s %d" % (base, i) for i in range(2, 1000) if "%s %d" % (base, i) not in have)


def ensure_active(cfg):
    """The settings being edited are always a preset: old configs get theirs saved as "My search". True if changed."""
    active = cfg.get("active_preset") or ""
    if active and preset_by_key(cfg, active) is not None:
        return False
    name = free_preset_name(cfg)
    cfg.setdefault("presets", {})[name] = preset_of(cfg)
    cfg["active_preset"] = name
    cfg["combined_presets"] = [k for k in cfg.get("combined_presets") or [] if k != name]
    return True


def active_set(cfg):
    """[key]: the preset being edited, then the combined ones (those that still exist, once each)."""
    out = []
    for key in [cfg.get("active_preset") or ""] + list(cfg.get("combined_presets") or []):
        if key and key not in out and preset_by_key(cfg, key) is not None:
            out.append(key)
    return out


def preset_label(key):
    return "%s (template)" % key[4:] if (key or "").startswith("tpl:") else key


def search_profiles(cfg):
    """[(label, profile)]: the preset being edited (the settings) first, then the other active ones. A posting is
    found by every profile's search and scored by each one's own scoring (the best counts)."""
    active = cfg.get("active_preset") or ""
    out, seen = [(preset_label(active).replace(", ", " ") if active else "settings", cfg)], {active}
    for key in cfg.get("combined_presets") or []:
        if key in seen:
            continue
        seen.add(key)
        preset = preset_by_key(cfg, key)
        if preset:
            out.append((preset_label(key).replace(", ", " "), preset))
    return out
