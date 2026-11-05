from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentkit import (
    CSVConnector,
    FieldSpec,
    MappingError,
    MappingSpec,
    SchemaMapper,
)

README_YAML = """
name: contacts
fields:
  email:
    compute: "lower(strip(email_address))"
    required: true
  name:
    from: full_name
    cast: str
    required: true
  stage:
    from: lifecycle
    default: lead
  score:
    from: lead_score
    cast: int
    default: 0
  label:
    compute: "concat(name, ' <', email, '>')"
"""


def _readme_mapper():
    return SchemaMapper.from_yaml(README_YAML)


def test_readme_scenario_rename_cast_default_compute_and_errors():
    raw = [
        {
            "full_name": "Maya Chen",
            "email_address": "Maya@Trailhead.io",
            "lifecycle": "customer",
            "lead_score": "12",
            "internal_id": "c_101",
        },
        {"full_name": "Riley Cho", "email_address": "riley@northwind.io", "lead_score": "n/a"},
    ]
    report = _readme_mapper().apply(raw)

    assert report.ok == 1 and report.failed == 1
    assert report.accepted_indexes == [0]
    maya = report.records[0]
    assert maya == {
        "email": "maya@trailhead.io",
        "name": "Maya Chen",
        "stage": "customer",
        "score": 12,
        "label": "Maya Chen <maya@trailhead.io>",
    }
    assert report.errors[0].index == 1
    assert report.errors[0].source["full_name"] == "Riley Cho"
    assert [(e.field, e.kind) for e in report.errors[0].errors] == [("score", "cast")]
    assert "cannot cast 'n/a' to int" in report.errors[0].errors[0].message
    assert str(report.errors[0]).startswith("record 1:")


def test_defaults_fill_blank_and_missing_but_not_real_values():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      score:
        from: lead_score
        cast: int
        default: 0
      stage:
        from: lifecycle
        default: lead
      phone:
        from: phone
        default: ""
    """)
    report = mapper.apply(
        [
            {},
            {"lead_score": "", "lifecycle": "   ", "phone": None},
            {"lead_score": "4", "lifecycle": "customer", "phone": "555"},
            {"lead_score": 0, "lifecycle": "lead", "phone": ""},
        ]
    )
    assert report.failed == 0
    assert report.records[0] == {"score": 0, "stage": "lead", "phone": ""}
    assert report.records[1] == {"score": 0, "stage": "lead", "phone": ""}
    assert report.records[2] == {"score": 4, "stage": "customer", "phone": "555"}
    # A present 0 is a real score. A blank phone is still missing, so the default fills it.
    assert report.records[3]["score"] == 0
    assert report.records[3]["phone"] == ""


def test_false_and_zero_are_not_treated_as_missing():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      active:
        from: active
        cast: bool
        default: true
      score:
        from: score
        cast: int
        default: 7
    """)
    assert mapper.map_one({"active": False, "score": 0}) == {"active": False, "score": 0}
    assert mapper.map_one({}) == {"active": True, "score": 7}


def test_optional_missing_field_is_null():
    mapper = SchemaMapper.from_yaml("name: t\nfields:\n  score:\n    from: score\n    cast: int\n")
    assert mapper.map_one({}) == {"score": None}
    assert mapper.map_one({"score": "8"}) == {"score": 8}


def test_missing_required_fields_are_reported_per_record():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      email:
        from: email
        required: true
      name:
        from: full_name
        required: true
    """)
    report = mapper.apply([{"email": "a@b.c"}, {"full_name": "Ada"}, {"email": "", "full_name": " "}])
    assert report.ok == 0 and report.failed == 3
    assert [e.field for e in report.errors[0].errors] == ["name"]
    assert report.errors[0].errors[0].kind == "required"
    assert [e.field for e in report.errors[1].errors] == ["email"]
    assert [e.field for e in report.errors[2].errors] == ["email", "name"]
    assert report.records == []


def test_bad_casts_do_not_drop_later_rows():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      email:
        from: email
        required: true
      score:
        from: score
        cast: int
        required: true
    """)
    rows = [
        {"email": "a@b.c", "score": "3"},
        {"email": "b@c.d", "score": "nope"},
        {"email": "c@d.e", "score": 4},
    ]
    report = mapper.apply(rows)
    assert [r["email"] for r in report.records] == ["a@b.c", "c@d.e"]
    assert report.records[0]["score"] == 3
    assert report.errors[0].index == 1
    assert report.errors[0].errors[0].value == "nope"
    assert rows[1] == {"email": "b@c.d", "score": "nope"}


def test_bool_date_datetime_and_decimal_casts():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      active:
        from: active
        cast: bool
        required: true
      joined:
        from: joined
        cast: date
        required: true
      seen:
        from: seen
        cast: datetime
        required: true
      mrr:
        from: mrr
        cast: decimal
        required: true
    """)
    row = mapper.map_one(
        {
            "active": "yes",
            "joined": "2026-10-20T15:04:05Z",
            "seen": "2026-10-20T15:04:05Z",
            "mrr": "12.50",
        }
    )
    assert row["active"] is True
    assert row["joined"] == date(2026, 10, 20)
    assert row["seen"] == datetime(2026, 10, 20, 15, 4, 5, tzinfo=UTC)
    assert row["mrr"] == Decimal("12.50")

    bad = mapper.apply([{"active": "maybe", "joined": "tomorrow", "seen": "nope", "mrr": "abc"}])
    assert bad.failed == 1
    assert {e.field for e in bad.errors[0].errors} == {"active", "joined", "seen", "mrr"}
    assert all(e.kind == "cast" for e in bad.errors[0].errors)


def test_nested_path_and_non_identifier_columns():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      account:
        from: account.name
        required: true
      email:
        compute: "lower(strip(row['E-mail']))"
        required: true
      tag:
        from: tags.0
    """)
    mapped = mapper.map_one(
        {"account": {"name": "Trailhead"}, "E-mail": " A@B.CO ", "tags": ["vip", "west"], "id": "1"}
    )
    assert mapped == {"account": "Trailhead", "email": "a@b.co", "tag": "vip"}


def test_compute_uses_earlier_fields_and_arithmetic():
    mapper = SchemaMapper.from_yaml("""
    name: t
    fields:
      score:
        from: lead_score
        cast: int
        required: true
      tier:
        compute: "'gold' if score >= 10 else 'std'"
      doubled:
        compute: "score * 2"
      phone:
        compute: "coalesce(phone, 'n/a')"
    """)
    mapped = mapper.map_one({"lead_score": "12", "phone": ""})
    assert mapped == {"score": 12, "tier": "gold", "doubled": 24, "phone": "n/a"}
    assert mapper.map_one({"lead_score": 3, "phone": "555"})["tier"] == "std"


def test_dependent_compute_is_not_a_second_error():
    mapper = _readme_mapper()
    report = mapper.apply([{"full_name": "Riley Cho", "lead_score": "n/a"}])
    assert [e.field for e in report.errors[0].errors] == ["email", "score"]


def test_compute_rejects_unsafe_and_unknown_syntax_at_load():
    for expr, match in [
        ("__import__('os')", "unknown function"),
        ("row.__class__", "private attribute"),
        ("lambda: 1", "unsupported syntax"),
        ("email.lower()", "method calls"),
    ]:
        with pytest.raises(MappingError, match=match):
            SchemaMapper.from_yaml(f"name: t\nfields:\n  value:\n    compute: {expr!r}\n")


def test_compute_runtime_guards():
    huge = SchemaMapper.from_yaml("name: t\nfields:\n  value:\n    compute: \"'a' * 100000\"\n")
    report = huge.apply([{}])
    assert report.failed == 1
    assert "string repeat" in report.errors[0].errors[0].message

    boom = SchemaMapper.from_yaml("name: t\nfields:\n  value:\n    compute: '1 / 0'\n")
    assert "division by zero" in boom.apply([{}]).errors[0].errors[0].message

    missing = SchemaMapper.from_yaml("name: t\nfields:\n  value:\n    compute: 'nope'\n")
    assert "unknown field" in missing.apply([{}]).errors[0].errors[0].message


def test_dry_run_diff_render_and_summary():
    mapper = SchemaMapper.from_yaml("""
    name: people
    fields:
      email:
        from: email_address
        cast: str
      score:
        from: lead_score
        cast: int
        default: 0
      label:
        compute: "concat(email, ':', score)"
    """)
    preview = mapper.dry_run(
        [
            {"email_address": "A@B.co", "lead_score": "4", "note": "x"},
            {"email_address": "c@d.co", "lead_score": "nope"},
        ]
    )
    assert preview.ok == 1 and preview.failed == 1
    assert preview.diffs[0].mapped["label"] == "A@B.co:4"
    assert preview.diffs[0].dropped == ("note",)
    assert preview.diffs[1].mapped is None
    assert preview.summary() == {
        "records": 2,
        "ok": 1,
        "failed": 1,
        "dropped": 1,
        "rename": 2,
        "cast": 1,
        "default": 0,
        "compute": 1,
    }
    expected = """\
record 0  ok
  rename   email: 'A@B.co' -> 'A@B.co'  (from email_address)
  cast     score: '4' -> 4  (from lead_score)
  compute  label: None -> 'A@B.co:4'
  dropped  note
record 1  ERROR
  rename   email: 'c@d.co' -> 'c@d.co'  (from email_address)
  error    score: cannot cast 'nope' to int
1 ok, 1 failed, 4 field change(s)"""
    assert preview.render() == expected
    # The preview reports the bad row; it does not raise.
    assert mapper.apply([{"email_address": "c@d.co", "lead_score": "nope"}]).failed == 1


def test_dry_run_shows_defaults_for_a_clean_row():
    text = _readme_mapper().dry_run([{"full_name": "Noah Blake", "email_address": "Noah@example.com"}]).render()
    assert "compute  email:" in text
    assert "rename   name:" in text
    assert "default  stage: None -> 'lead'" in text
    assert "default  score: None -> 0" in text
    assert "compute  label:" in text
    assert "Noah Blake <noah@example.com>" in text


def test_input_is_not_mutated_and_generator_is_single_pass():
    mapper = _readme_mapper()
    raw = {"full_name": "Ada", "email_address": "Ada@ex.io", "meta": {"n": 1}}
    mapper.apply([raw])
    assert raw == {"full_name": "Ada", "email_address": "Ada@ex.io", "meta": {"n": 1}}

    def rows():
        yield {"full_name": "Ada", "email_address": "a@ex.io"}
        yield {"full_name": "Bea", "email_address": "b@ex.io"}

    assert mapper.apply(rows()).ok == 2
    assert mapper.apply([]).ok == 0


def test_strict_false_keeps_unmapped_fields():
    mapper = SchemaMapper.from_yaml("""
    name: t
    strict: false
    fields:
      email:
        from: email_address
        required: true
    """)
    mapped = mapper.map_one({"email_address": "a@b.c", "note": "keep"})
    assert mapped == {"note": "keep", "email": "a@b.c"}
    diff = mapper.dry_run([{"email_address": "a@b.c", "note": "keep"}]).diffs[0]
    assert diff.dropped == ()


def test_non_dict_record_and_raise_mode():
    mapper = _readme_mapper()
    report = mapper.apply([{"full_name": "Ada", "email_address": "a@ex.io"}, "nope"])
    assert report.ok == 1
    assert report.errors[0].index == 1
    assert report.errors[0].errors[0].kind == "record"

    with pytest.raises(MappingError, match="cannot cast") as raised:
        mapper.apply(
            [
                {"full_name": "Ada", "email_address": "a@ex.io", "lead_score": "1"},
                {"full_name": "Bea", "email_address": "b@ex.io", "lead_score": "x"},
            ],
            on_error="raise",
        )
    assert raised.value.report.ok == 1
    assert raised.value.errors[0].index == 1
    with pytest.raises(MappingError, match="n/a"):
        mapper.map_one({"full_name": "Riley", "email_address": "r@x.io", "lead_score": "n/a"})


def test_invalid_specs_and_yaml():
    with pytest.raises(MappingError, match="invalid mapping YAML"):
        SchemaMapper.from_yaml("fields: [")
    with pytest.raises(MappingError, match="must be a mapping"):
        SchemaMapper.from_yaml("- just\n- a list\n")
    with pytest.raises(MappingError, match="from"):
        SchemaMapper.from_yaml("name: t\nfields:\n  email:\n    from: email\n    compute: lower(email)\n")
    with pytest.raises(MappingError, match="integer|cast"):
        SchemaMapper.from_yaml("name: t\nfields:\n  score:\n    from: score\n    cast: integer\n")
    with pytest.raises(MappingError, match="bogus"):
        SchemaMapper.from_yaml("name: t\nbogus: 1\nfields:\n  email:\n    from: email\n")
    with pytest.raises(MappingError, match="usable name"):
        SchemaMapper.from_yaml('name: t\nfields:\n  "E-mail":\n    from: email\n')
    with pytest.raises(MappingError, match="at least one"):
        SchemaMapper.from_yaml("name: t\nfields: {}\n")
    with pytest.raises(MappingError, match="default"):
        SchemaMapper.from_yaml("name: t\nfields:\n  score:\n    from: score\n    cast: int\n    default: n/a\n")
    with pytest.raises(MappingError, match="cannot read"):
        SchemaMapper.from_file("/tmp/does-not-exist-agentkit-mapping.yaml")


def test_list_form_from_file_and_python_spec(tmp_path):
    path = tmp_path / "contacts.yaml"
    path.write_text(
        """
name: contacts
fields:
  - name: email
    from: email_address
    required: true
  - target: score
    from: lead_score
    cast: int
    default: 0
""",
        encoding="utf-8",
    )
    mapped = SchemaMapper.from_file(path).map_one({"email_address": "a@b.c"})
    assert mapped == {"email": "a@b.c", "score": 0}

    spec = MappingSpec(
        name="contacts",
        fields={"email": FieldSpec(source="email_address", required=True)},
    )
    assert SchemaMapper(spec).map_one({"email_address": "a@b.c"}) == {"email": "a@b.c"}
    assert "default" not in FieldSpec(source="email").model_fields_set
    assert "default" in FieldSpec(source="email", default=None).model_fields_set


def test_yaml_bool_and_string_defaults():
    as_bool = SchemaMapper.from_yaml("name: t\nfields:\n  flag:\n    from: flag\n    cast: bool\n    default: no\n")
    as_text = SchemaMapper.from_yaml(
        'name: t\nfields:\n  flag:\n    from: flag\n    cast: str\n    default: "no"\n'
    )
    assert as_bool.map_one({}) == {"flag": False}
    assert as_text.map_one({}) == {"flag": "no"}


def test_generated_model_is_a_strict_pydantic_check():
    mapper = SchemaMapper.from_yaml("name: t\nfields:\n  score:\n    from: score\n    cast: int\n    required: true\n")
    with pytest.raises(ValidationError):
        mapper._model.model_validate({"score": "1"})
    with pytest.raises(ValidationError):
        mapper._model.model_validate({"score": 1, "extra": True})


def test_maps_a_csv_export(tmp_path):
    csv = CSVConnector(tmp_path)
    csv.write(
        "contacts",
        [
            {"E-mail": "Maya@Trailhead.io", "Name": "Maya Chen", "Score": "12"},
            {"E-mail": "riley@northwind.io", "Name": "Riley Cho", "Score": "n/a"},
        ],
    )
    mapper = SchemaMapper.from_yaml("""
    name: csv_contacts
    fields:
      email:
        compute: "lower(row['E-mail'])"
        required: true
      name:
        from: Name
        required: true
      score:
        from: Score
        cast: int
        required: true
    """)
    report = mapper.apply(CSVConnector(tmp_path).read("contacts"))
    assert report.ok == 1 and report.failed == 1
    assert report.records[0] == {"email": "maya@trailhead.io", "name": "Maya Chen", "score": 12}
    assert report.errors[0].index == 1
    assert report.errors[0].errors[0].kind == "cast"


def test_raise_for_errors_and_bad_on_error():
    mapper = _readme_mapper()
    ok = mapper.apply([{"full_name": "Ada", "email_address": "a@ex.io"}])
    assert ok.raise_for_errors() is ok
    bad = mapper.apply([{"full_name": "Riley", "lead_score": "n/a"}])
    with pytest.raises(MappingError):
        bad.raise_for_errors()
    with pytest.raises(MappingError, match="on_error"):
        mapper.apply([], on_error="explode")
