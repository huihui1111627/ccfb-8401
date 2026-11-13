"""ctplatform 命令行。

子命令：
  init-db                 建库并写入种子代码表/模板
  generate-samples        生成测试报文（含脏数据）到 samples 目录
  ingest <file> [-t 模板] 接入单个数据源文件
  run                     重跑全量流水线（归一化/链/对账/统计）
  list <表名>             查看表内容
  web [--host --port]     启动网页控制台
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import CONFIG
from .db import connect
from .ingest import ingest_file
from .normalize import run_pipeline, seed_all
from .testdata import build_dataset, build_excel_sample


def cmd_init_db(args):
    con = connect()
    result = seed_all(con)
    print(f"初始化完成：代码表 {result['codes']} 条，映射模板 {result['templates']} 个")
    con.close()


def cmd_generate_samples(args):
    CONFIG.ensure_dirs()
    out = CONFIG.sample_dir
    n = 0
    for name, data in build_dataset().items():
        (out / name).write_bytes(data)
        n += 1
    xlsx_name, xlsx_bytes = build_excel_sample()
    (out / xlsx_name).write_bytes(xlsx_bytes)
    n += 1
    print(f"已生成 {n} 个测试文件到 {out}")


def cmd_ingest(args):
    path = Path(args.file)
    data = path.read_bytes()
    con = connect()
    try:
        result = ingest_file(path.name, data, template_id=args.template, con=con)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.run:
            print(json.dumps(run_pipeline(con), ensure_ascii=False, indent=2))
    finally:
        con.close()


def cmd_run(args):
    con = connect()
    try:
        print(json.dumps(run_pipeline(con), ensure_ascii=False, indent=2, default=str))
    finally:
        con.close()


def cmd_list(args):
    con = connect(read_only=True)
    try:
        rows = con.execute(f"SELECT * FROM {args.table} LIMIT {args.limit}").fetchdf()
        if rows.empty:
            print("(空)")
        else:
            print(rows.to_string(index=False))
    finally:
        con.close()


def cmd_web(args):
    from .web.app import create_app
    app = create_app()
    app.run(host=args.host, port=args.port, debug=False)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ctplatform", description="码头箱动态数据平台")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="建库并写入种子数据").set_defaults(func=cmd_init_db)
    sub.add_parser("generate-samples", help="生成测试报文").set_defaults(func=cmd_generate_samples)

    p_ing = sub.add_parser("ingest", help="接入数据源文件")
    p_ing.add_argument("file")
    p_ing.add_argument("-t", "--template", help="CSV/Excel 的映射模板 ID")
    p_ing.add_argument("--run", action="store_true", help="接入后立即重跑流水线")
    p_ing.set_defaults(func=cmd_ingest)

    sub.add_parser("run", help="重跑全量流水线").set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="查看表内容")
    p_list.add_argument("table", choices=[
        "source_file", "raw_record", "mapping_template", "mapping_field", "code_table",
        "normalized_event", "field_lineage", "event_chain", "recon_diff",
        "voyage_stats", "pipeline_run"])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_web = sub.add_parser("web", help="启动网页控制台")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=5000)
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
