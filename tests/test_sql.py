import sqlite3

import pytest

from agentkit import ConnectorError, SQLConnector


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "crm.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tier TEXT, email TEXT UNIQUE);
        INSERT INTO customers VALUES (1, 'Acme', 'gold', 'ops@acme.test');
        INSERT INTO customers VALUES (2, 'Globex', 'free', 'it@globex.test');
        INSERT INTO customers VALUES (3, 'Initech', 'gold', NULL);
        CREATE TABLE events (ts TEXT, kind TEXT);
        INSERT INTO events VALUES ('2026-10-01', 'login'), ('2026-10-02', 'login');
        """
    )
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def test_read_all_and_filters(db):
    c = SQLConnector(db)
    assert [r["name"] for r in c.read("customers")] == ["Acme", "Globex", "Initech"]
    assert [r["id"] for r in c.read("customers", tier="gold")] == [1, 3]
    assert [r["id"] for r in c.read("customers", email=None)] == [3]


def test_columns_and_limit(db):
    c = SQLConnector(db)
    assert list(c.read("customers", columns=["name"], limit=2)) == [{"name": "Acme"}, {"name": "Globex"}]


def test_keyset_batches_cover_every_row_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'big.db'}"
    c = SQLConnector(url, batch_size=7)
    con = sqlite3.connect(tmp_path / "big.db")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    con.commit()
    con.close()
    c.write("t", [{"id": i, "v": i * 2} for i in range(1, 51)])
    ids = [r["id"] for r in c.read("t")]
    assert ids == list(range(1, 51))
    assert [r["id"] for r in c.read("t", limit=10)] == list(range(1, 11))


def test_order_by_unique_and_non_unique(db):
    c = SQLConnector(db, batch_size=1)
    assert [r["email"] for r in c.read("customers", order_by="email", columns=["email"])] == [
        None,
        "it@globex.test",
        "ops@acme.test",
    ]
    assert [r["name"] for r in c.read("customers", order_by="tier")][-1] == "Initech"


def test_table_without_pk_needs_order_by(db):
    c = SQLConnector(db)
    with pytest.raises(ConnectorError, match="primary key"):
        list(c.read("events"))
    assert len(list(c.read("events", order_by="ts"))) == 2


def test_unknown_table_and_columns_rejected(db):
    c = SQLConnector(db)
    with pytest.raises(ConnectorError, match="no such table"):
        list(c.read("customers; DROP TABLE customers"))
    with pytest.raises(ConnectorError, match="unknown filter columns"):
        list(c.read("customers", **{"1=1 OR id": 1}))
    with pytest.raises(ConnectorError, match="unknown columns"):
        c.write("customers", [{"id": 9, "name": "x", "evil": 1}])
    assert len(list(c.read("customers"))) == 3


def test_filter_values_are_bound_not_formatted(db):
    c = SQLConnector(db)
    assert list(c.read("customers", name="Acme' OR '1'='1")) == []


def test_write_is_all_or_nothing(db):
    c = SQLConnector(db)
    with pytest.raises(ConnectorError, match="rolled back"):
        c.write("customers", [{"id": 4, "name": "Hooli"}, {"id": 1, "name": "dup pk"}])
    assert [r["id"] for r in c.read("customers")] == [1, 2, 3]


def test_upsert_updates_and_inserts(db):
    c = SQLConnector(db)
    n = c.write(
        "customers",
        [{"id": 1, "name": "Acme Corp", "tier": "platinum"}, {"id": 4, "name": "Hooli", "tier": "free"}],
        upsert_on="id",
    )
    assert n == 2
    rows = {r["id"]: r for r in c.read("customers")}
    assert rows[1]["name"] == "Acme Corp" and rows[1]["email"] == "ops@acme.test"
    assert rows[4]["tier"] == "free"
    # re-running the same sync is idempotent
    c.write("customers", [{"id": 4, "name": "Hooli", "tier": "free"}], upsert_on="id")
    assert len(list(c.read("customers"))) == 4


def test_upsert_requires_key(db):
    with pytest.raises(ConnectorError, match="needs 'id'"):
        SQLConnector(db).write("customers", [{"name": "no id"}], upsert_on="id")


def test_read_only_blocks_writes(db):
    c = SQLConnector(db, read_only=True)
    with pytest.raises(ConnectorError, match="read-only"):
        c.write("customers", [{"id": 5, "name": "x"}])


def test_query_select_only_with_params(db):
    c = SQLConnector(db)
    rows = c.query("SELECT tier, COUNT(*) AS n FROM customers WHERE id > :min GROUP BY tier ORDER BY tier", min=0)
    assert rows == [{"tier": "free", "n": 1}, {"tier": "gold", "n": 2}]
    for bad in ["DELETE FROM customers", "SELECT 1; DROP TABLE customers", "PRAGMA table_info(x)"]:
        with pytest.raises(ConnectorError, match="single SELECT"):
            c.query(bad)


def test_schema_tables_and_health(db):
    c = SQLConnector(db)
    assert c.tables() == ["customers", "events"]
    assert c.schema("customers") == {"id": "INTEGER", "name": "TEXT", "tier": "TEXT", "email": "TEXT"}
    assert c.health_check()
    c.close()


def test_url_from_env(db, monkeypatch):
    monkeypatch.setenv("CUSTOMER_DB_URL", db)
    with SQLConnector(url_env="CUSTOMER_DB_URL") as c:
        assert len(list(c.read("customers"))) == 3
    monkeypatch.delenv("CUSTOMER_DB_URL")
    with pytest.raises(ConnectorError, match="not set"):
        SQLConnector(url_env="CUSTOMER_DB_URL")
    with pytest.raises(ConnectorError, match="pass url"):
        SQLConnector()


def test_empty_write(db):
    assert SQLConnector(db).write("customers", []) == 0
