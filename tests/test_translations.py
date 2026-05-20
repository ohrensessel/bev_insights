"""Translation coverage tests.

Verifies that de.json mirrors the structure of en.json so a new sensor
can't land without its German name, and that the options/issues sections
are present in both files.
"""
from __future__ import annotations

import json
from pathlib import Path

TRANSLATIONS = Path(__file__).parent.parent / "custom_components/bev_insights/translations"


def _load(lang: str) -> dict:
    with (TRANSLATIONS / f"{lang}.json").open() as fh:
        return json.load(fh)


def test_de_entity_keys_match_en() -> None:
    """Every sensor entity key in en.json must appear in de.json and vice versa."""
    en_keys = set(_load("en")["entity"]["sensor"].keys())
    de_keys = set(_load("de")["entity"]["sensor"].keys())
    missing_de = en_keys - de_keys
    extra_de = de_keys - en_keys
    assert not missing_de, f"de.json missing entity keys: {missing_de}"
    assert not extra_de, f"de.json has extra entity keys not in en.json: {extra_de}"


def test_de_state_attributes_match_en() -> None:
    """Every state_attributes block in en.json must be reproduced in de.json."""
    en_sensors = _load("en")["entity"]["sensor"]
    de_sensors = _load("de")["entity"]["sensor"]
    mismatches: list[str] = []
    for key, en_entry in en_sensors.items():
        en_attrs = set(en_entry.get("state_attributes", {}).keys())
        de_attrs = set(de_sensors.get(key, {}).get("state_attributes", {}).keys())
        missing = en_attrs - de_attrs
        if missing:
            mismatches.append(f"{key}: missing {missing}")
    assert not mismatches, "de.json state_attributes gaps:\n" + "\n".join(mismatches)


def test_required_sections_present_in_all_langs() -> None:
    """config, options, and issues sections must exist in every translation file."""
    for lang in ("en", "de"):
        data = _load(lang)
        for section in ("config", "options", "issues"):
            assert section in data, f"{lang}.json missing top-level '{section}'"


def test_options_data_keys_match_en() -> None:
    """de.json options data keys must match en.json."""
    en_data = _load("en")["options"]["step"]["init"]["data"]
    de_data = _load("de")["options"]["step"]["init"]["data"]
    assert set(en_data) == set(de_data), (
        f"Options data key mismatch — en: {set(en_data)}, de: {set(de_data)}"
    )
