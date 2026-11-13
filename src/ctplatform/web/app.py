"""Flask 网页控制台。

页面：
  /            总览仪表盘
  /sources     数据源接入（上传、文件清单、解析错误）
  /mappings    映射规则（模板列映射 + 统一代码表）
  /movements   箱动态（事件查询 + 每箱动态链）
  /recon       对账差异
  /lineage     字段血缘（事件 -> 报文/文件 -> 段/列/行号）
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash

from ..config import CONFIG
from ..db import connect
from ..ingest import ingest_file
from ..normalize import run_pipeline


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = "ctplatform-dev-key"
    app.config["DB_PATH"] = db_path or str(CONFIG.db_path)

    def db():
        cfg = CONFIG
        if db_path:
            from ..config import Config
            cfg = Config(home=str(Path(db_path).parent))
            cfg.db_path = Path(db_path)
        return connect(cfg)

    @app.route("/")
    def dashboard():
        con = db()
        kpi = {
            "files": con.execute("SELECT count(*) FROM source_file").fetchone()[0],
            "files_failed": con.execute(
                "SELECT count(*) FROM source_file WHERE parse_status='ERROR'").fetchone()[0],
            "events": con.execute("SELECT count(*) FROM normalized_event").fetchone()[0],
            "duplicates": con.execute(
                "SELECT count(*) FROM normalized_event WHERE is_duplicate").fetchone()[0],
            "containers": con.execute(
                "SELECT count(DISTINCT container_no) FROM normalized_event").fetchone()[0],
            "diffs": con.execute("SELECT count(*) FROM recon_diff").fetchone()[0],
            "chains_bad": con.execute(
                "SELECT count(*) FROM event_chain WHERE chain_status<>'OK'").fetchone()[0],
        }
        chain_dist = con.execute(
            "SELECT chain_status, count(*) FROM event_chain GROUP BY chain_status ORDER BY 2 DESC"
        ).fetchall()
        diff_dist = con.execute(
            "SELECT diff_type, severity, count(*) c FROM recon_diff GROUP BY 1,2 ORDER BY c DESC"
        ).fetchall()
        recent_runs = con.execute(
            "SELECT run_id, started_at, status, files_seen, events, duplicates, diffs "
            "FROM pipeline_run ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
        con.close()
        return render_template("dashboard.html", kpi=kpi, chain_dist=chain_dist,
                               diff_dist=diff_dist, recent_runs=recent_runs)

    @app.route("/sources", methods=["GET", "POST"])
    def sources():
        con = db()
        if request.method == "POST":
            file = request.files.get("data_file")
            template_id = request.form.get("template_id") or None
            if not file:
                flash("请选择文件", "error")
            else:
                try:
                    result = ingest_file(file.filename, file.read(),
                                         template_id=template_id, con=con)
                    if result["status"] == "DUPLICATE_FILE":
                        flash(f"文件已存在（SHA256 去重）：{result['file_id']}", "warn")
                    else:
                        run_pipeline(con)
                        flash(f"接入成功：{result['source_type']}，"
                              f"{result['records']} 条原始记录，状态 {result['parse_status']}",
                              "ok")
                except Exception as exc:
                    flash(f"接入失败：{exc}", "error")
            return redirect(url_for("sources"))
        files = con.execute(
            "SELECT file_id, filename, source_type, message_type, template_id, "
            "byte_size, parse_status, parse_errors, ingested_at "
            "FROM source_file ORDER BY ingested_at DESC"
        ).fetchall()
        templates = con.execute(
            "SELECT template_id, template_name, source_type FROM mapping_template"
        ).fetchall()
        con.close()
        files_view = []
        for f in files:
            files_view.append({
                "file_id": f[0], "filename": f[1], "source_type": f[2],
                "message_type": f[3], "template_id": f[4], "byte_size": f[5],
                "parse_status": f[6],
                "parse_errors": json.loads(f[7] or "[]"), "ingested_at": f[8],
            })
        return render_template("sources.html", files=files_view, templates=templates)

    @app.route("/sources/<file_id>")
    def source_detail(file_id):
        con = db()
        f = con.execute("SELECT * FROM source_file WHERE file_id=?", [file_id]).fetchone()
        records = con.execute(
            "SELECT raw_id, record_index, segment_line, raw_payload, record_status "
            "FROM raw_record WHERE file_id=? ORDER BY record_index", [file_id]).fetchall()
        con.close()
        if not f:
            flash("文件不存在", "error")
            return redirect(url_for("sources"))
        recs = [{"raw_id": r[0], "index": r[1], "line": r[2],
                 "payload": json.dumps(json.loads(r[3]), ensure_ascii=False, indent=2),
                 "status": r[4]} for r in records]
        return render_template("source_detail.html", f=f,
                               errors=json.loads(f[9] or "[]"), records=recs)

    @app.route("/mappings")
    def mappings():
        con = db()
        templates = con.execute(
            """SELECT t.template_id, t.template_name, t.source_type,
                      count(m.mapping_id) AS field_cnt
               FROM mapping_template t LEFT JOIN mapping_field m
               ON t.template_id=m.template_id GROUP BY 1,2,3 ORDER BY 1""").fetchall()
        fields = con.execute(
            """SELECT m.template_id, m.source_column, m.target_field, m.transform
               FROM mapping_field m ORDER BY m.template_id, m.mapping_id""").fetchall()
        codes = con.execute(
            """SELECT domain, source_system, raw_code, std_code, std_name
               FROM code_table ORDER BY domain, source_system, raw_code""").fetchall()
        con.close()
        return render_template("mappings.html", templates=templates,
                               fields=fields, codes=codes)

    @app.route("/movements")
    def movements():
        con = db()
        container_no = request.args.get("container_no", "").strip()
        event_type = request.args.get("event_type", "").strip()
        where, params = [], []
        if container_no:
            where.append("container_no LIKE ?")
            params.append(f"%{container_no}%")
        if event_type:
            where.append("event_type=?")
            params.append(event_type)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        events = con.execute(
            f"""SELECT event_id, container_no, event_type, event_time, port_code,
                       vessel_name, voyage_no, container_type, size_ft, laden_status,
                       is_duplicate, quality_flags, message_type, file_id, segment_line
                FROM normalized_event {where_sql}
                ORDER BY container_no, event_time NULLS LAST LIMIT 300""", params).fetchall()
        chains = con.execute(
            """SELECT c.container_no, c.event_count, c.event_types, c.chain_status, c.issues
               FROM event_chain c
               WHERE (? = '' OR c.container_no LIKE ?)
               ORDER BY c.chain_status DESC, c.container_no LIMIT 100""",
            [container_no, f"%{container_no}%"]).fetchall()
        con.close()
        evs = [{"event_id": e[0], "container_no": e[1], "event_type": e[2],
                "event_time": e[3], "port_code": e[4], "vessel_name": e[5],
                "voyage_no": e[6], "container_type": e[7], "size_ft": e[8],
                "laden_status": e[9], "is_duplicate": e[10],
                "quality_flags": json.loads(e[11]), "message_type": e[12],
                "file_id": e[13], "segment_line": e[14]} for e in events]
        chs = [{"container_no": c[0], "count": c[1],
                "event_types": json.loads(c[2]), "status": c[3],
                "issues": json.loads(c[4])} for c in chains]
        return render_template("movements.html", events=evs, chains=chs,
                               filters={"container_no": container_no, "event_type": event_type})

    @app.route("/recon")
    def recon():
        con = db()
        severity = request.args.get("severity", "").strip()
        diff_type = request.args.get("diff_type", "").strip()
        where, params = [], []
        if severity:
            where.append("severity=?")
            params.append(severity)
        if diff_type:
            where.append("diff_type=?")
            params.append(diff_type)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        diffs = con.execute(
            f"""SELECT diff_id, diff_type, severity, container_no, file_id,
                       event_id, description, detail, detected_at
                FROM recon_diff {where_sql}
                ORDER BY CASE severity WHEN 'ERROR' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
                         detected_at DESC LIMIT 500""", params).fetchall()
        types = con.execute(
            "SELECT DISTINCT diff_type FROM recon_diff ORDER BY 1").fetchall()
        stats = con.execute(
            "SELECT vessel_name, voyage_no, port_code, load_count, discharge_count, "
            "load_teu, discharge_teu, gate_in_count, gate_out_count "
            "FROM voyage_stats ORDER BY vessel_name, port_code").fetchall()
        con.close()
        dvs = [{"id": d[0], "type": d[1], "severity": d[2], "container_no": d[3],
                "file_id": d[4], "event_id": d[5], "desc": d[6],
                "detail": json.loads(d[7] or "{}"), "at": d[8]} for d in diffs]
        return render_template("recon.html", diffs=dvs,
                               types=[t[0] for t in types], stats=stats,
                               filters={"severity": severity, "diff_type": diff_type})

    @app.route("/lineage")
    def lineage():
        con = db()
        event_id = request.args.get("event_id", "").strip()
        container_no = request.args.get("container_no", "").strip()
        if not event_id and container_no:
            row = con.execute(
                "SELECT event_id FROM normalized_event WHERE container_no=? "
                "ORDER BY event_time LIMIT 1", [container_no]).fetchone()
            event_id = row[0] if row else ""
        event = None
        lineage_rows = []
        if event_id:
            event = con.execute(
                "SELECT event_id, container_no, event_type, event_time, file_id, "
                "raw_id, message_type, segment_line FROM normalized_event "
                "WHERE event_id=?", [event_id]).fetchone()
            lineage_rows = con.execute(
                """SELECT fl.target_field, fl.source_kind, fl.source_locator,
                          fl.source_line, fl.raw_value, fl.std_value,
                          sf.filename, sf.file_id
                   FROM field_lineage fl
                   JOIN raw_record rr ON fl.raw_id=rr.raw_id
                   JOIN source_file sf ON rr.file_id=sf.file_id
                   WHERE fl.event_id=? ORDER BY fl.target_field""",
                [event_id]).fetchall()
        candidates = con.execute(
            "SELECT event_id, container_no, event_type, event_time "
            "FROM normalized_event ORDER BY container_no, event_time LIMIT 50").fetchall()
        con.close()
        lns = [{"field": l[0], "kind": l[1], "locator": l[2], "line": l[3],
                "raw": l[4], "std": l[5], "filename": l[6], "file_id": l[7]}
               for l in lineage_rows]
        return render_template("lineage.html", event=event, lineage=lns,
                               candidates=candidates, event_id=event_id,
                               container_no=container_no)

    return app
