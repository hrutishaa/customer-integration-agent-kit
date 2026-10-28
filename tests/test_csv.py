import pytest

from agentkit import ConnectorError, CSVConnector


def test_write_then_read(tmp_path):
    c = CSVConnector(tmp_path)
    assert c.write("customers", [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Globex"}]) == 2
    assert list(c.read("customers")) == [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Globex"}]


def test_append_keeps_header(tmp_path):
    c = CSVConnector(tmp_path)
    c.write("t", [{"id": "1", "name": "a"}])
    c.write("t", [{"name": "b", "id": "2"}])
    assert [r["name"] for r in c.read("t")] == ["a", "b"]
    assert (tmp_path / "t.csv").read_text().count("id,name") == 1


def test_filters(tmp_path):
    c = CSVConnector(tmp_path)
    c.write("t", [{"id": "1", "tier": "gold"}, {"id": "2", "tier": "free"}])
    assert [r["id"] for r in c.read("t", tier="gold")] == ["1"]


def test_unknown_column_rejected(tmp_path):
    c = CSVConnector(tmp_path)
    c.write("t", [{"id": "1"}])
    with pytest.raises(ConnectorError, match="unknown columns"):
        c.write("t", [{"id": "2", "evil": "x"}])


def test_missing_resource(tmp_path):
    with pytest.raises(ConnectorError, match="no such resource"):
        list(CSVConnector(tmp_path).read("nope"))


def test_path_traversal_blocked(tmp_path):
    with pytest.raises(ConnectorError, match="escapes"):
        list(CSVConnector(tmp_path / "data").read("../secrets"))


def test_empty_write_and_health(tmp_path):
    c = CSVConnector(tmp_path)
    assert c.write("t", []) == 0
    assert c.health_check()
    assert not CSVConnector(tmp_path / "missing").health_check()
