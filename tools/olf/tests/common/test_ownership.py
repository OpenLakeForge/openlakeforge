from openlakeforge_services.ownership import marker_of, marker_value, patch_operations


def test_equal_entities_need_no_patch() -> None:
    current = {"description": "d", "domains": [{"id": "1", "name": "sales"}], "extension": {"olfManaged": "git:a"}}
    desired = {"description": "d", "domains": [{"id": "1", "type": "domain"}], "extension": {"olfManaged": "git:a"}}

    assert patch_operations(current, desired) == []


def test_only_named_fields_are_patched_and_extension_keys_merge() -> None:
    current = {"description": "old", "tags": [{"tagFQN": "PII.None"}], "extension": {"reviewer": "ana"}}

    operations = patch_operations(current, {"description": "new", "extension": {"olfManaged": "git:a"}})

    assert operations == [
        {"op": "replace", "path": "/description", "value": "new"},
        {"op": "add", "path": "/extension/olfManaged", "value": "git:a"},
    ]


def test_an_empty_wanted_value_on_an_absent_field_is_no_difference() -> None:
    assert patch_operations({}, {"description": "", "extension": {"olfManaged": "git:a"}}) == [
        {"op": "add", "path": "/extension", "value": {"olfManaged": "git:a"}}
    ]


def test_marker_round_trips_and_ignores_other_extensions() -> None:
    assert marker_of({"extension": {"olfManaged": marker_value("abc")}}) == "git:abc"
    assert marker_of({"extension": {"olfManaged": "someone"}}) is None
    assert marker_of({}) is None
    assert marker_value("") == "git:unknown"
