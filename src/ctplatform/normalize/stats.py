"""按船次/航线统计装卸量（自然箱数 + TEU）。

同箱同船次同港的同类事件只计一次（跨来源/重复报文天然去重），
每箱取最大尺寸作为 TEU 口径，避免来源间尺寸差异造成重复计数。
"""
from __future__ import annotations


def rebuild_stats(con) -> int:
    con.execute("DELETE FROM voyage_stats")
    con.execute(
        """
        WITH ranked AS (
            SELECT
                container_no, event_type, vessel_imo, vessel_name, voyage_no,
                port_code, coalesce(max(size_ft), 20) AS size_ft,
                row_number() OVER (
                    PARTITION BY container_no, event_type,
                                 vessel_imo, voyage_no, port_code
                    ORDER BY event_time NULLS LAST
                ) AS rn
            FROM normalized_event
            WHERE event_type IN ('LOAD','DISCHARGE','GATE_IN','GATE_OUT')
              AND container_no <> '__MISSING_CONTAINER__'
            GROUP BY 1,2,3,4,5,6, event_time
        ),
        dedup AS (
            SELECT * FROM ranked WHERE rn = 1
        )
        INSERT INTO voyage_stats
        (stats_id, vessel_imo, vessel_name, voyage_no, port_code, route_key,
         load_count, discharge_count, load_teu, discharge_teu,
         gate_in_count, gate_out_count)
        SELECT
            'vs_' || md5(coalesce(vessel_imo,'?') || '|' || coalesce(voyage_no,'?')
                         || '|' || coalesce(port_code,'?')),
            vessel_imo,
            any_value(vessel_name),
            voyage_no,
            port_code,
            coalesce(vessel_imo,'?') || '/' || coalesce(voyage_no,'?')
                || '@' || coalesce(port_code,'?'),
            count_if(event_type='LOAD'),
            count_if(event_type='DISCHARGE'),
            sum(CASE WHEN event_type='LOAD' THEN size_ft / 20.0 ELSE 0 END),
            sum(CASE WHEN event_type='DISCHARGE' THEN size_ft / 20.0 ELSE 0 END),
            count_if(event_type='GATE_IN'),
            count_if(event_type='GATE_OUT')
        FROM dedup
        GROUP BY vessel_imo, voyage_no, port_code
        """
    )
    return con.execute("SELECT count(*) FROM voyage_stats").fetchone()[0]
