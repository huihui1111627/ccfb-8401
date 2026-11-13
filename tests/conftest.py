import sys
from pathlib import Path

import pytest

# 兼容 `python -I -m pytest`：-I 会剥离 cwd，显式把 src 加回 sys.path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ctplatform.config import Config  # noqa: E402
from ctplatform.db import connect  # noqa: E402
from ctplatform.ingest import ingest_file  # noqa: E402
from ctplatform.normalize import run_pipeline, seed_all  # noqa: E402
from ctplatform.testdata import build_dataset, build_excel_sample  # noqa: E402


@pytest.fixture()
def con(tmp_path):
    """每个用例一个独立 DuckDB 文件。"""
    cfg = Config(home=str(tmp_path / "home"))
    cfg.db_path = tmp_path / "test.duckdb"
    c = connect(cfg)
    yield c
    c.close()


@pytest.fixture()
def loaded(con):
    """接入全部造数文件并跑完流水线，返回 (connection, 摘要)。"""
    seed_all(con)
    files = build_dataset()
    summaries = []
    for name, data in files.items():
        tpl = "tpl_gate_csv" if name.endswith(".csv") else None
        summaries.append(ingest_file(name, data, template_id=tpl, con=con))
    xlsx_name, xlsx_bytes = build_excel_sample()
    summaries.append(ingest_file(xlsx_name, xlsx_bytes,
                                 template_id="tpl_ops_excel", con=con))
    result = run_pipeline(con)
    return con, result, summaries
