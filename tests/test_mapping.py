from pathlib import Path

import pytest
from pydantic import BaseModel, field_validator

from agentkit import Mapper, MappingError, load_mapping

EXAMPLE = Path(__file__).parent.parent / "examples" / "contacts.yaml"

ROW = {"Email Address": "  Ada@Example.COM ", "First Name": "Ada", "Last Name": " Lovelace",
       "Phone": "(555) 010-9999", "Company": " Analytical ", "Stage": " WON ",
       "Deal Value": "$1,250.50", "Active?": "Yes"}


def test_example_mapping_renames_casts_and_computes():
    rec, errs = load_mapping(EXAMPLE).map_record(ROW)
    assert errs == []
    assert rec == {"email": "ada@example.com", "name": "Ada Lovelace", "phone": "5550109999",
                   "company": "Analytical", "stage": "won", "amount": 1250.5, "active": True}


def test_defaults_fill_missing_and_empty_values():
    rec, errs = load_mapping(EXAMPLE).map_record(
        {"Email Address": "a@b.co", "First Name": "A", "Last Name": "B", "Stage": "", "Deal Value": ""})
    assert errs == [] and rec["stage"] == "lead" and rec["amount"] == 0 and rec["active"] is True
    assert rec["phone"] is None


def test_bad_records_are_reported_not_raised():
    rows = [ROW, {**ROW, "Email Address": " "}, {**ROW, "Deal Value": "lots"}, {**ROW, "Active?": "maybe"}]
    result = load_mapping(EXAMPLE).apply(rows)
    assert len(result.records) == 1 and result.skipped == [1, 2, 3]
    assert [(e.index, e.field) for e in result.errors] == [(1, "email"), (2, "amount"), (3, "active")]
    assert "cannot cast 'lots' to float" in result.report()


def test_all_errors_in_one_record_are_collected():
    result = load_mapping(EXAMPLE).apply([{**ROW, "Email Address": "", "Deal Value": "x"}])
    assert {e.field for e in result.errors} == {"email", "amount"}


def test_missing_source_column_is_clear():
    m = Mapper({"fields": {"id": {"from": "ID", "required": True}}})
    _, errs = m.map_record({"Other": 1})
    assert "source column 'ID' not found" in errs[0].message


def test_compute_with_missing_column_errors():
    m = Mapper({"fields": {"n": {"compute": "{a} {b}"}}})
    _, errs = m.map_record({"a": "x"})
    assert "'b' not found" in errs[0].message


def test_compute_is_a_template_not_eval():
    m = Mapper({"fields": {"n": {"compute": "{a}"}}})
    rec, _ = m.map_record({"a": "__import__('os').getcwd()"})
    assert rec["n"] == "__import__('os').getcwd()"


def test_int_cast_rejects_fractions():
    m = Mapper({"fields": {"qty": {"from": "q", "cast": "int"}}})
    assert m.map_record({"q": "1,200"})[0] == {"qty": 1200}
    assert m.map_record({"q": 2.5})[0] is None


def test_date_cast_normalizes_iso_timestamps():
    m = Mapper({"fields": {"d": {"from": "d", "cast": "date"}}})
    assert m.map_record({"d": "2026-03-04T10:00:00Z"})[0] == {"d": "2026-03-04"}
    assert m.map_record({"d": "03/04/2026"})[0] is None


class Contact(BaseModel):
    email: str
    name: str
    amount: float

    @field_validator("email")
    @classmethod
    def has_at(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("not an email address")
        return v


def test_pydantic_validation_adds_per_record_errors():
    m = Mapper({"fields": {"email": {"from": "e"}, "name": {"from": "n"}, "amount": {"from": "a", "cast": "float"}}},
               validate=Contact)
    result = m.apply([{"e": "a@b.co", "n": "A", "a": "1"}, {"e": "nope", "n": "B", "a": "2"}, {"e": "c@d.co", "n": "C"}])
    assert len(result.records) == 1
    assert [(e.index, e.field) for e in result.errors] == [(1, "email"), (2, "amount")]
    assert result.skipped == [1, 2]  # record 2 has no amount, which the model requires


@pytest.mark.parametrize("spec, msg", [
    ({"fields": {"a": {}}}, "needs 'from'"),
    ({"fields": {"a": {"from": "x", "compute": "{x}"}}}, "not both"),
    ({"fields": {"a": {"from": "x", "cast": "decimal"}}}, "cast"),
    ({"fields": {"a": {"from": "x", "bogus": 1}}}, "bogus"),
    ({"fields": {}}, "empty"),
    ({"nope": 1}, "fields"),
    ({"fields": {"a": {"from": "x"}}, "extra": 1}, "unknown top-level"),
])
def test_invalid_mappings_fail_loudly(spec, msg):
    with pytest.raises(MappingError, match=msg):
        Mapper(spec)


def test_load_mapping_from_string_and_bad_yaml(tmp_path):
    assert load_mapping("fields:\n  a: {from: x}\n").map_record({"x": 1})[0] == {"a": 1}
    with pytest.raises(MappingError, match="invalid YAML"):
        load_mapping("fields: [unclosed")
    with pytest.raises(MappingError, match="cannot read"):
        load_mapping(tmp_path / "missing.yaml")


def test_dry_run_creates_updates_and_unchanged():
    m = load_mapping(EXAMPLE)
    existing = [
        {"email": "ada@example.com", "name": "Ada Lovelace", "phone": "5550109999", "company": "Analytical",
         "stage": "won", "amount": 1250.5, "active": True},
        {"email": "bob@example.com", "name": "Bob", "stage": "lead", "amount": 0},
    ]
    rows = [ROW,
            {**ROW, "Email Address": "bob@example.com", "First Name": "Bob", "Last Name": "", "Stage": "won",
             "Phone": "", "Company": "", "Deal Value": "10", "Active?": ""},
            {**ROW, "Email Address": "new@example.com"},
            {**ROW, "Email Address": ""}]
    run = m.dry_run(rows, key="email", existing=existing)
    assert run.counts() == {"create": 1, "update": 1, "unchanged": 1, "rejected": 1}
    update = next(c for c in run.changes if c.action == "update")
    assert update.diff["stage"] == ("lead", "won") and update.diff["amount"] == (0, 10.0)
    text = run.report()
    assert "+ new@example.com" in text and "~ bob@example.com" in text and "! record 3" in text


def test_dry_run_flags_duplicate_keys_and_bad_key():
    m = load_mapping(EXAMPLE)
    run = m.dry_run([ROW, ROW], key="email")
    assert run.counts()["create"] == 1 and "duplicate key" in run.errors[0].message
    with pytest.raises(MappingError, match="not a mapped field"):
        m.dry_run([ROW], key="id")


def test_dry_run_skips_rejected_rows_without_misaligning_indexes():
    m = load_mapping(EXAMPLE)
    run = m.dry_run([{**ROW, "Email Address": ""}, {**ROW, "Email Address": "z@z.co"}], key="email")
    assert [(c.index, c.key) for c in run.changes] == [(1, "z@z.co")]
    assert run.errors[0].index == 0
