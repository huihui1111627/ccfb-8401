"""流水线编排：接入 -> 归一化 -> 动态链 -> 对账 -> 统计。

派生数据全量重算，任意时刻重跑结果一致（幂等）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from ..db import connect
from .codes import seed_all
from .normalize import rebuild_normalized
from .chains import rebuild_chains, rebuild_reconciliation
from .stats import rebuild_stats


def run_pipeline(con=None) -> dict:
    own = con is None
    con = con or connect()
    run_id = "run_" + hashlib.sha256(str(datetime.now()).encode()).hexdigest()[:14]
    started = datetime.now()
    con.execute(
        "INSERT INTO pipeline_run (run_id, started_at, status) VALUES (?,?, 'RUNNING')",
        [run_id, started],
    )
    try:
        seed = seed_all(con)
        norm = rebuild_normalized(con)
        chain_stats = rebuild_chains(con)
        recon = rebuild_reconciliation(con)
        stats_rows = rebuild_stats(con)
        files = con.execute(
            "SELECT count(*), count_if(parse_status='ERROR') FROM source_file"
        ).fetchone()
        finished = datetime.now()
        con.execute(
            """UPDATE pipeline_run SET
            finished_at=?, files_seen=?, files_failed=?, raw_records=?,
            events=?, duplicates=?, diffs=?, status='OK' WHERE run_id=?""",
            [finished, files[0], files[1],
             con.execute("SELECT count(*) FROM raw_record").fetchone()[0],
             norm["events"], norm["duplicates"], recon["diffs"], run_id],
        )
        result = {
            "run_id": run_id, "seed": seed, **norm, **chain_stats, **recon,
            "voyage_stats_rows": stats_rows, "elapsed_sec": (finished - started).total_seconds(),
        }
        return result
    except Exception as exc:
        con.execute(
            "UPDATE pipeline_run SET finished_at=?, status='ERROR' WHERE run_id=?",
            [datetime.now(), run_id],
        )
        raise exc
    finally:
        if own:
            con.close()
