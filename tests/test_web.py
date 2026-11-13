"""网页控制台冒烟测试：所有页面 200，关键内容渲染。"""
import pytest
from io import BytesIO

from ctplatform.config import Config
from ctplatform.web.app import create_app


@pytest.fixture()
def db_path(tmp_path):
    cfg = Config(home=str(tmp_path / "home"))
    cfg.db_path = tmp_path / "web_test.duckdb"
    return str(cfg.db_path)


@pytest.fixture()
def loaded_db(db_path):
    """独立于 conftest：直接在 db_path 上跑完整接入流水线。"""
    from ctplatform.db import connect
    from ctplatform.ingest import ingest_file
    from ctplatform.normalize import run_pipeline, seed_all
    from ctplatform.testdata import build_dataset, build_excel_sample

    cfg = Config(home=str(db_path).rsplit("/", 1)[0])
    cfg.db_path = db_path
    con = connect(cfg)
    seed_all(con)
    for name, data in build_dataset().items():
        tpl = "tpl_gate_csv" if name.endswith(".csv") else None
        ingest_file(name, data, template_id=tpl, con=con)
    xlsx_name, xlsx_bytes = build_excel_sample()
    ingest_file(xlsx_name, xlsx_bytes, template_id="tpl_ops_excel", con=con)
    run_pipeline(con)
    con.close()
    return db_path


@pytest.fixture()
def client(loaded_db):
    app = create_app(db_path=loaded_db)
    app.config["TESTING"] = True
    return app.test_client()


def test_dashboard(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "总览仪表盘".encode() in resp.data


def test_sources_page(client):
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "BAPLIE_20261109.edi".encode() in resp.data


def test_source_detail(client, loaded_db):
    from ctplatform.db import connect
    from ctplatform.config import Config
    cfg = Config(home=loaded_db.rsplit("/", 1)[0]); cfg.db_path = loaded_db
    con = connect(cfg, read_only=True)
    file_id = con.execute(
        "SELECT file_id FROM source_file WHERE filename='BAPLIE_20261109.edi'").fetchone()[0]
    con.close()
    resp = client.get(f"/sources/{file_id}")
    assert resp.status_code == 200
    assert "EGLV1000001".encode() in resp.data


def test_mappings_page(client):
    resp = client.get("/mappings")
    assert resp.status_code == 200
    assert "统一代码表".encode() in resp.data


def test_movements_page(client):
    resp = client.get("/movements")
    assert resp.status_code == 200
    assert "EGLV1000001".encode() in resp.data


def test_recon_page(client):
    resp = client.get("/recon")
    assert resp.status_code == 200
    assert "船次航线装卸量统计".encode() in resp.data


def test_lineage_page(client, loaded_db):
    from ctplatform.db import connect
    from ctplatform.config import Config
    cfg = Config(home=loaded_db.rsplit("/", 1)[0]); cfg.db_path = loaded_db
    con = connect(cfg, read_only=True)
    event_id = con.execute(
        "SELECT event_id FROM normalized_event WHERE container_no='EGLV1000001' LIMIT 1"
    ).fetchone()[0]
    con.close()
    resp = client.get(f"/lineage?event_id={event_id}")
    assert resp.status_code == 200
    assert "port_code".encode() in resp.data


def test_upload_csv_through_web(loaded_db):
    app = create_app(db_path=loaded_db)
    app.config["TESTING"] = True
    client = app.test_client()
    header = "箱号,船公司,箱型,事件,时间,港口,船名,航次,IMO,箱态\n"
    row = "EGLV1000099,EGLV,22G1,GATE_IN,2026-11-10 06:00:00,CNSHA,EVER_GLORY,V-X,IMO9, FULL\n"
    data = {
        "data_file": (BytesIO((header + row).encode()), "web_upload.csv"),
        "template_id": "tpl_gate_csv",
    }
    resp = client.post("/sources", data=data, content_type="multipart/form-data",
                       follow_redirects=True)
    assert resp.status_code == 200
    from ctplatform.db import connect
    from ctplatform.config import Config
    cfg = Config(home=loaded_db.rsplit("/", 1)[0]); cfg.db_path = loaded_db
    con = connect(cfg, read_only=True)
    assert con.execute(
        "SELECT count(*) FROM normalized_event WHERE container_no='EGLV1000099'"
    ).fetchone()[0] == 1
    con.close()
