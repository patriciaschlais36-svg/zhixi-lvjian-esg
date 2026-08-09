from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


项目根目录 = Path(__file__).resolve().parents[1]
默认种子数据库 = 项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite"
默认运行目录 = 项目根目录 / "平台运行数据"
数据库扩展脚本 = Path(__file__).with_name("平台数据库扩展.sql")
默认指标文件 = 项目根目录 / "算法源码" / "配置" / "ESG指标体系.json"

命名空间 = uuid.UUID("7cf26953-93dc-5f0f-bdbd-b00fb11cdca6")


def 当前时间() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def 稳定编号(前缀: str, 键: str) -> str:
    return f"{前缀}_{uuid.uuid5(命名空间, 键).hex}"


def 随机编号(前缀: str) -> str:
    return f"{前缀}_{uuid.uuid4().hex}"


def 文件摘要(路径: Path, 块大小: int = 1024 * 1024) -> tuple[str, int]:
    摘要器 = hashlib.sha256()
    大小 = 0
    with 路径.open("rb") as 文件:
        while 数据 := 文件.read(块大小):
            摘要器.update(数据)
            大小 += len(数据)
    return 摘要器.hexdigest(), 大小


def _JSON文本(值: Any) -> str:
    return json.dumps(值, ensure_ascii=False, separators=(",", ":"))


def _环境路径(名称: str, 默认值: Path) -> Path:
    配置值 = os.environ.get(名称, "").strip()
    return Path(配置值 or 默认值).resolve()


@dataclass(frozen=True)
class 平台路径:
    种子数据库: Path
    运行目录: Path
    运行数据库: Path
    上传目录: Path
    任务目录: Path
    指标文件: Path

    @classmethod
    def 从环境变量(cls) -> "平台路径":
        运行目录 = _环境路径("ESG_PLATFORM_RUNTIME_DIR", 默认运行目录)
        return cls(
            种子数据库=_环境路径("ESG_PLATFORM_SEED_DB", 默认种子数据库),
            运行目录=运行目录,
            运行数据库=_环境路径("ESG_PLATFORM_DB", 运行目录 / "平台数据库.sqlite"),
            上传目录=_环境路径("ESG_PLATFORM_UPLOAD_DIR", 运行目录 / "上传报告"),
            任务目录=_环境路径("ESG_PLATFORM_JOB_DIR", 运行目录 / "分析任务"),
            指标文件=_环境路径("ESG_INDICATOR_JSON", 默认指标文件),
        )


class 数据服务:
    def __init__(self, 路径: 平台路径 | None = None) -> None:
        self.路径 = 路径 or 平台路径.从环境变量()

    def 初始化(self) -> None:
        self.路径.运行目录.mkdir(parents=True, exist_ok=True)
        self.路径.上传目录.mkdir(parents=True, exist_ok=True)
        self.路径.任务目录.mkdir(parents=True, exist_ok=True)
        if not self.路径.运行数据库.exists():
            if not self.路径.种子数据库.is_file():
                raise FileNotFoundError(f"未找到平台种子数据库：{self.路径.种子数据库}")
            临时库 = self.路径.运行数据库.with_suffix(".初始化中")
            shutil.copy2(self.路径.种子数据库, 临时库)
            临时库.replace(self.路径.运行数据库)

        self._迁移报告年份约束()
        self._应用数据库扩展()
        with self.写连接() as 连接:
            self._载入指标目录(连接)
            连接.execute(
                """
                UPDATE analysis_job
                   SET status='interrupted', stage='服务重启后待人工重试',
                       error_code='SERVICE_RESTART',
                       error_message='服务重启时任务仍处于运行状态，未将其误判为成功。',
                       progress=100, finished_at=?, updated_at=?
                 WHERE status='running'
                """,
                (当前时间(), 当前时间()),
            )
            连接.execute(
                "INSERT INTO platform_event VALUES (?, 'service_initialized', 'database', NULL, '{}', ?)",
                (随机编号("event"), 当前时间()),
            )

        with self.读连接() as 连接:
            完整性 = 连接.execute("PRAGMA integrity_check").fetchone()[0]
            外键错误 = 连接.execute("PRAGMA foreign_key_check").fetchall()
        if 完整性 != "ok" or 外键错误:
            raise RuntimeError(f"平台数据库初始化验收失败：integrity={完整性}, foreign_keys={len(外键错误)}")

    def _应用数据库扩展(self) -> None:
        连接 = sqlite3.connect(self.路径.运行数据库, timeout=30)
        try:
            连接.execute("PRAGMA foreign_keys=ON")
            连接.execute("PRAGMA journal_mode=WAL")
            连接.execute("PRAGMA busy_timeout=30000")
            当前版本 = int(连接.execute("PRAGMA user_version").fetchone()[0])
            if 当前版本 not in (2, 3):
                raise RuntimeError(f"不支持的平台数据库基础版本：{当前版本}")
            脚本 = 数据库扩展脚本.read_text(encoding="utf-8")
            连接.executescript(f"BEGIN IMMEDIATE;\n{脚本}\nCOMMIT;")
        except Exception:
            if 连接.in_transaction:
                连接.rollback()
            raise
        finally:
            连接.close()

    def _连接(self) -> sqlite3.Connection:
        连接 = sqlite3.connect(self.路径.运行数据库, timeout=30, check_same_thread=False)
        连接.row_factory = sqlite3.Row
        连接.execute("PRAGMA foreign_keys=ON")
        连接.execute("PRAGMA journal_mode=WAL")
        连接.execute("PRAGMA busy_timeout=30000")
        return 连接

    @contextmanager
    def 读连接(self) -> Iterator[sqlite3.Connection]:
        连接 = self._连接()
        try:
            yield 连接
        finally:
            连接.close()

    @contextmanager
    def 写连接(self) -> Iterator[sqlite3.Connection]:
        连接 = self._连接()
        try:
            连接.execute("BEGIN IMMEDIATE")
            yield 连接
            连接.commit()
        except Exception:
            连接.rollback()
            raise
        finally:
            连接.close()

    def _迁移报告年份约束(self) -> None:
        连接 = sqlite3.connect(self.路径.运行数据库, timeout=30)
        try:
            建表语句行 = 连接.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='report'"
            ).fetchone()
            if not 建表语句行:
                raise RuntimeError("种子数据库缺少 report 表")
            建表语句 = 建表语句行[0]
            if "report_year BETWEEN 2000 AND 2100" in 建表语句:
                return
            if "report_year IN (2023, 2024, 2025)" not in 建表语句:
                raise RuntimeError("检测到未知的 report 年份约束，已停止自动迁移")

            连接.execute("PRAGMA foreign_keys=OFF")
            连接.execute("BEGIN IMMEDIATE")
            连接.execute(
                """
                CREATE TABLE report_new (
                  report_id TEXT PRIMARY KEY,
                  company_id TEXT NOT NULL REFERENCES company(company_id),
                  report_year INTEGER NOT NULL CHECK(report_year BETWEEN 2000 AND 2100),
                  report_type_key TEXT NOT NULL,
                  primary_report_type TEXT NOT NULL
                    CHECK(primary_report_type IN ('ESG', 'SD', 'CSR', 'ENV', 'OTHER')),
                  language_code TEXT NOT NULL DEFAULT 'zh-CN',
                  scope_code TEXT NOT NULL DEFAULT 'unknown',
                  edition_no INTEGER NOT NULL DEFAULT 1 CHECK(edition_no >= 1),
                  source_site TEXT,
                  source_announcement_id TEXT,
                  canonical_title TEXT NOT NULL,
                  logical_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'superseded', 'withdrawn', 'review')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(company_id, report_year, report_type_key,
                         language_code, scope_code, edition_no)
                )
                """
            )
            连接.execute("INSERT INTO report_new SELECT * FROM report")
            连接.execute("DROP TABLE report")
            连接.execute("ALTER TABLE report_new RENAME TO report")
            连接.commit()
        except Exception:
            连接.rollback()
            raise
        finally:
            连接.close()

    def _载入指标目录(self, 连接: sqlite3.Connection) -> None:
        载荷 = json.loads(self.路径.指标文件.read_text(encoding="utf-8"))
        指标 = 载荷.get("indicators", [])
        if len(指标) != 80:
            raise ValueError(f"指标体系数量异常：应为80，实际为{len(指标)}")
        版本 = "全国赛80项指标体系"
        for 项 in 指标:
            别名: list[str] = []
            for 键 in ("aliases_cn", "aliases_en"):
                值 = 项.get(键, "")
                if isinstance(值, str):
                    别名.extend(x.strip() for x in 值.split(";") if x.strip())
                elif isinstance(值, list):
                    别名.extend(str(x).strip() for x in 值 if str(x).strip())
            连接.execute(
                """
                INSERT INTO indicator_catalog(
                    indicator_id, metric_name_cn, dimension, metric_type,
                    extraction_priority, unit_normalized, definition,
                    aliases_json, source_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(indicator_id) DO UPDATE SET
                    metric_name_cn=excluded.metric_name_cn,
                    dimension=excluded.dimension,
                    metric_type=excluded.metric_type,
                    extraction_priority=excluded.extraction_priority,
                    unit_normalized=excluded.unit_normalized,
                    definition=excluded.definition,
                    aliases_json=excluded.aliases_json,
                    source_version=excluded.source_version
                """,
                (
                    项["field_id"], 项["metric_name_cn"], 项["dimension"],
                    项["metric_type"], 项["extraction_priority"],
                    项.get("unit_normalized"), 项.get("definition"),
                    _JSON文本(sorted(set(别名))), 版本,
                ),
            )

    def 就绪状态(self) -> dict[str, Any]:
        with self.读连接() as 连接:
            数据 = {
                "database_integrity": 连接.execute("PRAGMA integrity_check").fetchone()[0],
                "companies": 连接.execute("SELECT COUNT(*) FROM company").fetchone()[0],
                "reports": 连接.execute("SELECT COUNT(*) FROM report").fetchone()[0],
                "indicators": 连接.execute("SELECT COUNT(*) FROM indicator_catalog").fetchone()[0],
                "jobs": 连接.execute("SELECT COUNT(*) FROM analysis_job").fetchone()[0],
                "results": 连接.execute("SELECT COUNT(*) FROM extraction_result").fetchone()[0],
            }
        数据["ready"] = 数据["database_integrity"] == "ok" and 数据["indicators"] == 80
        return 数据

    def 概览(self) -> dict[str, Any]:
        with self.读连接() as 连接:
            年份 = [dict(x) for x in 连接.execute(
                "SELECT report_year AS year, COUNT(*) AS reports FROM report GROUP BY report_year ORDER BY report_year"
            )]
            状态 = [dict(x) for x in 连接.execute(
                "SELECT status, COUNT(*) AS count FROM analysis_job GROUP BY status ORDER BY status"
            )]
            return {
                "company_count": 连接.execute("SELECT COUNT(*) FROM company").fetchone()[0],
                "report_count": 连接.execute("SELECT COUNT(*) FROM report").fetchone()[0],
                "indicator_count": 连接.execute("SELECT COUNT(*) FROM indicator_catalog").fetchone()[0],
                "result_count": 连接.execute("SELECT COUNT(*) FROM extraction_result").fetchone()[0],
                "evidence_count": 连接.execute("SELECT COUNT(*) FROM evidence_span").fetchone()[0],
                "report_years": 年份,
                "job_statuses": 状态,
            }

    def 企业列表(self, 关键词: str = "", 页码: int = 1, 每页: int = 20) -> dict[str, Any]:
        页码, 每页 = max(1, 页码), min(100, max(1, 每页))
        条件, 参数 = "", []
        if 关键词:
            条件 = "WHERE c.stock_code LIKE ? OR c.current_short_name LIKE ? OR c.legal_name LIKE ?"
            模式 = f"%{关键词}%"
            参数 = [模式, 模式, 模式]
        with self.读连接() as 连接:
            总数 = 连接.execute(f"SELECT COUNT(*) FROM company c {条件}", 参数).fetchone()[0]
            行 = 连接.execute(
                f"""
                SELECT c.company_id, c.stock_code, c.current_short_name, c.legal_name,
                       COUNT(DISTINCT r.report_id) AS report_count,
                       MIN(r.report_year) AS first_year, MAX(r.report_year) AS latest_year,
                       COUNT(DISTINCT er.result_id) AS result_count
                  FROM company c
                  LEFT JOIN report r ON r.company_id=c.company_id
                  LEFT JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                  LEFT JOIN extraction_result er ON er.report_version_id=rv.report_version_id
                  {条件}
                 GROUP BY c.company_id
                 ORDER BY c.stock_code
                 LIMIT ? OFFSET ?
                """,
                [*参数, 每页, (页码 - 1) * 每页],
            ).fetchall()
        return {"items": [dict(x) for x in 行], "page": 页码, "page_size": 每页, "total": 总数}

    def 企业详情(self, 企业编号: str) -> dict[str, Any] | None:
        with self.读连接() as 连接:
            企业 = 连接.execute("SELECT * FROM company WHERE company_id=?", (企业编号,)).fetchone()
            if not 企业:
                return None
            报告 = 连接.execute(
                """
                SELECT r.report_id, r.report_year, r.canonical_title, r.primary_report_type,
                       r.status, rv.report_version_id, rv.verification_status,
                       COUNT(DISTINCT er.result_id) AS result_count
                  FROM report r
                  JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                  LEFT JOIN extraction_result er ON er.report_version_id=rv.report_version_id
                 WHERE r.company_id=?
                 GROUP BY r.report_id, rv.report_version_id
                 ORDER BY r.report_year DESC
                """,
                (企业编号,),
            ).fetchall()
        返回 = dict(企业)
        返回["reports"] = [dict(x) for x in 报告]
        return 返回

    def 报告列表(
        self, 企业编号: str | None = None, 年份: int | None = None,
        关键词: str = "", 页码: int = 1, 每页: int = 20,
    ) -> dict[str, Any]:
        页码, 每页 = max(1, 页码), min(100, max(1, 每页))
        条件, 参数 = [], []
        if 企业编号:
            条件.append("c.company_id=?"); 参数.append(企业编号)
        if 年份:
            条件.append("r.report_year=?"); 参数.append(年份)
        if 关键词:
            条件.append("(c.stock_code LIKE ? OR c.current_short_name LIKE ? OR r.canonical_title LIKE ?)")
            模式 = f"%{关键词}%"; 参数.extend([模式, 模式, 模式])
        where = "WHERE " + " AND ".join(条件) if 条件 else ""
        基础 = f"""
            FROM report r JOIN company c ON c.company_id=r.company_id
            JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
            LEFT JOIN analysis_job j ON j.report_version_id=rv.report_version_id
            {where}
        """
        with self.读连接() as 连接:
            总数 = 连接.execute(f"SELECT COUNT(DISTINCT r.report_id) {基础}", 参数).fetchone()[0]
            行 = 连接.execute(
                f"""
                SELECT r.report_id, rv.report_version_id, r.report_year, r.canonical_title,
                       r.primary_report_type, r.status, c.company_id, c.stock_code,
                       c.current_short_name, rv.verification_status,
                       MAX(j.created_at) AS latest_job_at
                  {基础}
                 GROUP BY r.report_id, rv.report_version_id
                 ORDER BY r.report_year DESC, c.stock_code
                 LIMIT ? OFFSET ?
                """,
                [*参数, 每页, (页码 - 1) * 每页],
            ).fetchall()
        return {"items": [dict(x) for x in 行], "page": 页码, "page_size": 每页, "total": 总数}

    def 报告详情(self, 报告版本编号: str) -> dict[str, Any] | None:
        with self.读连接() as 连接:
            行 = 连接.execute(
                """
                SELECT r.report_id, rv.report_version_id, r.report_year, r.canonical_title,
                       r.primary_report_type, r.status, c.company_id, c.stock_code,
                       c.current_short_name, rv.original_file_name, rv.verification_status,
                       rv.quality_flags_json, fb.sha256, fb.file_size_bytes,
                       fb.pdf_header_ok, fb.pdf_eof_ok
                  FROM report_version rv
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN company c ON c.company_id=r.company_id
                  JOIN file_blob fb ON fb.file_blob_id=rv.file_blob_id
                 WHERE rv.report_version_id=?
                """,
                (报告版本编号,),
            ).fetchone()
            if not 行:
                return None
            任务 = [dict(x) for x in 连接.execute(
                "SELECT * FROM analysis_job WHERE report_version_id=? ORDER BY created_at DESC",
                (报告版本编号,),
            )]
        返回 = dict(行)
        返回["quality_flags"] = json.loads(返回.pop("quality_flags_json") or "[]")
        返回["jobs"] = 任务
        return 返回

    def 指标列表(self, 维度: str | None = None, 优先级: str | None = None) -> list[dict[str, Any]]:
        条件, 参数 = [], []
        if 维度:
            条件.append("dimension=?"); 参数.append(维度)
        if 优先级:
            条件.append("extraction_priority=?"); 参数.append(优先级)
        where = "WHERE " + " AND ".join(条件) if 条件 else ""
        with self.读连接() as 连接:
            行 = 连接.execute(
                f"SELECT * FROM indicator_catalog {where} ORDER BY dimension, indicator_id", 参数
            ).fetchall()
        返回 = []
        for 项 in 行:
            数据 = dict(项); 数据["aliases"] = json.loads(数据.pop("aliases_json")); 返回.append(数据)
        return 返回

    def _验证PDF(self, 路径: Path, 最大字节: int) -> tuple[str, int, bool]:
        大小 = 路径.stat().st_size
        if 大小 <= 0 or 大小 > 最大字节:
            raise ValueError(f"PDF文件大小不合法，允许范围为1至{最大字节}字节")
        with 路径.open("rb") as 文件:
            头 = 文件.read(5)
            文件.seek(max(0, 大小 - 4096))
            尾 = 文件.read()
        if 头 != b"%PDF-":
            raise ValueError("文件扩展名为PDF，但文件头不是PDF")
        摘要, _ = 文件摘要(路径)
        return 摘要, 大小, b"%%EOF" in 尾

    def 登记上传并创建任务(
        self, 临时文件: Path, *, 股票代码: str, 报告年份: int,
        企业简称: str, 报告标题: str, 原始文件名: str,
        请求幂等键: str | None = None,
        最大字节: int = 30 * 1024 * 1024,
    ) -> dict[str, Any]:
        if len(股票代码) != 6 or not 股票代码.isdigit():
            raise ValueError("股票代码必须为6位数字")
        if not 2000 <= int(报告年份) <= 2100:
            raise ValueError("报告年份必须在2000至2100之间")
        企业简称, 报告标题 = 企业简称.strip(), 报告标题.strip()
        if not 企业简称 or not 报告标题:
            raise ValueError("企业简称和报告标题不能为空")

        摘要, 大小, 尾标记正常 = self._验证PDF(临时文件, 最大字节)
        最终文件 = self.路径.上传目录 / f"{摘要}.pdf"
        if not 最终文件.exists():
            临时副本 = 最终文件.with_suffix(".写入中")
            shutil.copy2(临时文件, 临时副本)
            临时副本.replace(最终文件)

        时间 = 当前时间()
        with self.写连接() as 连接:
            if 请求幂等键:
                请求幂等键 = 请求幂等键.strip()
                if not 1 <= len(请求幂等键) <= 200:
                    raise ValueError("请求幂等键长度必须在1至200字符之间")
                已有任务 = 连接.execute(
                    """
                    SELECT j.job_id, j.run_id, j.report_version_id, fb.sha256, fb.pdf_eof_ok,
                           c.stock_code, r.report_year
                      FROM analysis_job j
                      JOIN report_version rv ON rv.report_version_id=j.report_version_id
                      JOIN file_blob fb ON fb.file_blob_id=rv.file_blob_id
                      JOIN report r ON r.report_id=rv.report_id
                      JOIN company c ON c.company_id=r.company_id
                     WHERE j.request_key=?
                    """,
                    (请求幂等键,),
                ).fetchone()
                if 已有任务:
                    if 已有任务["sha256"] != 摘要:
                        raise ValueError("同一请求幂等键已用于不同文件")
                    if (
                        已有任务["stock_code"] != 股票代码
                        or int(已有任务["report_year"]) != int(报告年份)
                    ):
                        raise ValueError("同一请求幂等键已用于不同报告元数据")
                    return {
                        "job_id": 已有任务["job_id"],
                        "run_id": 已有任务["run_id"],
                        "report_version_id": 已有任务["report_version_id"],
                        "sha256": 已有任务["sha256"],
                        "deduplication": {"blob": True, "report_version": True, "job": True},
                        "pdf_eof_ok": bool(已有任务["pdf_eof_ok"]),
                    }
            已有内容 = 连接.execute(
                """
                SELECT fb.file_blob_id, rv.report_version_id, r.report_year, c.stock_code
                  FROM file_blob fb
                  JOIN report_version rv ON rv.file_blob_id=fb.file_blob_id
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN company c ON c.company_id=r.company_id
                 WHERE fb.sha256=?
                 ORDER BY rv.is_current DESC, rv.created_at DESC LIMIT 1
                """,
                (摘要,),
            ).fetchone()
            if 已有内容 and (
                已有内容["stock_code"] != 股票代码 or int(已有内容["report_year"]) != int(报告年份)
            ):
                raise ValueError("相同文件内容已登记为另一公司或年份，请核对上传元数据")

            if 已有内容:
                报告版本编号 = 已有内容["report_version_id"]
                连接.execute(
                    """
                    INSERT OR IGNORE INTO file_location(
                        location_id, file_blob_id, report_version_id, root_code,
                        relative_path, observed_at, is_available
                    ) VALUES (?, ?, ?, 'UPLOADS', ?, ?, 1)
                    """,
                    (
                        稳定编号("location", f"UPLOADS|{摘要}.pdf"), 已有内容["file_blob_id"],
                        报告版本编号, f"{摘要}.pdf", 时间,
                    ),
                )
                任务编号 = 随机编号("job")
                运行编号 = 随机编号("run")
                连接.execute(
                    """
                    INSERT INTO analysis_job(
                        job_id, report_version_id, run_id, status, stage, progress,
                        attempt, runner_mode, pipeline_version, created_at, updated_at,
                        request_key
                    ) VALUES (?, ?, ?, 'queued', '等待真实抽取流水线', 0, 1,
                              'live_pipeline', '智析绿鉴可信抽取引擎', ?, ?, ?)
                    """,
                    (任务编号, 报告版本编号, 运行编号, 时间, 时间, 请求幂等键),
                )
                连接.execute(
                    "INSERT INTO platform_event VALUES (?, 'report_uploaded', 'analysis_job', ?, ?, ?)",
                    (
                        随机编号("event"), 任务编号,
                        _JSON文本({"sha256": 摘要, "deduplication": {"blob": True, "report_version": True, "job": False}}),
                        时间,
                    ),
                )
                return {
                    "job_id": 任务编号,
                    "run_id": 运行编号,
                    "report_version_id": 报告版本编号,
                    "sha256": 摘要,
                    "deduplication": {"blob": True, "report_version": True, "job": False},
                    "pdf_eof_ok": 尾标记正常,
                }

            企业 = 连接.execute(
                "SELECT company_id FROM company WHERE exchange='SSE' AND stock_code=?", (股票代码,)
            ).fetchone()
            企业编号 = 企业[0] if 企业 else 稳定编号("company", f"SSE|{股票代码}")
            if not 企业:
                连接.execute(
                    "INSERT INTO company VALUES (?, 'SSE', ?, ?, ?, 'unknown', ?, ?)",
                    (企业编号, 股票代码, 企业简称, 企业简称, 时间, 时间),
                )
            else:
                连接.execute(
                    "UPDATE company SET current_short_name=COALESCE(NULLIF(current_short_name,''), ?), updated_at=? WHERE company_id=?",
                    (企业简称, 时间, 企业编号),
                )

            逻辑键 = f"SSE|{股票代码}|{报告年份}|ESG|zh-CN|company|1"
            报告 = 连接.execute("SELECT report_id FROM report WHERE logical_key=?", (逻辑键,)).fetchone()
            报告编号 = 报告[0] if 报告 else 稳定编号("report", 逻辑键)
            if not 报告:
                连接.execute(
                    """
                    INSERT INTO report VALUES (
                        ?, ?, ?, 'ESG', 'ESG', 'zh-CN', 'company', 1,
                        'user_upload', NULL, ?, ?, 'active', ?, ?
                    )
                    """,
                    (报告编号, 企业编号, 报告年份, 报告标题, 逻辑键, 时间, 时间),
                )
                连接.execute("INSERT OR IGNORE INTO report_type_tag VALUES (?, 'ESG')", (报告编号,))

            内容 = 连接.execute("SELECT file_blob_id FROM file_blob WHERE sha256=?", (摘要,)).fetchone()
            内容编号 = 内容[0] if 内容 else 稳定编号("blob", 摘要)
            if not 内容:
                连接.execute(
                    """
                    INSERT INTO file_blob VALUES (?, ?, ?, 'application/pdf', 1, ?, 'computed', ?, ?)
                    """,
                    (内容编号, 摘要, 大小, int(尾标记正常), 最终文件.stat().st_mtime_ns, 时间),
                )

            版本 = 连接.execute(
                """
                SELECT rv.report_version_id, rv.file_blob_id, rv.version_no
                  FROM report_version rv WHERE rv.report_id=? AND rv.is_current=1
                """,
                (报告编号,),
            ).fetchone()
            重复使用 = bool(版本 and 版本["file_blob_id"] == 内容编号)
            if 重复使用:
                报告版本编号 = 版本["report_version_id"]
            else:
                版本号 = (版本["version_no"] + 1) if 版本 else 1
                if 版本:
                    连接.execute("UPDATE report_version SET is_current=0 WHERE report_version_id=?", (版本["report_version_id"],))
                报告版本编号 = 稳定编号("version", f"{报告编号}|{摘要}")
                连接.execute(
                    """
                    INSERT INTO report_version VALUES (
                        ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 1, ?, ?, ?, ?
                    )
                    """,
                    (
                        报告版本编号, 报告编号, 内容编号, 版本号, 摘要,
                        原始文件名, 企业简称, "revised" if 版本 else "initial",
                        "accepted" if 尾标记正常 else "review",
                        _JSON文本([] if 尾标记正常 else ["pdf_eof_missing"]), 时间,
                    ),
                )
                连接.execute(
                    "INSERT INTO file_location VALUES (?, ?, ?, 'UPLOADS', ?, ?, 1)",
                    (
                        稳定编号("location", f"UPLOADS|{摘要}.pdf"), 内容编号,
                        报告版本编号, f"{摘要}.pdf", 时间,
                    ),
                )

            任务编号 = 随机编号("job")
            运行编号 = 随机编号("run")
            连接.execute(
                """
                INSERT INTO analysis_job(
                    job_id, report_version_id, run_id, status, stage, progress,
                    attempt, runner_mode, pipeline_version, created_at, updated_at,
                    request_key
                ) VALUES (?, ?, ?, 'queued', '等待真实抽取流水线', 0, 1,
                          'live_pipeline', '智析绿鉴可信抽取引擎', ?, ?, ?)
                """,
                (任务编号, 报告版本编号, 运行编号, 时间, 时间, 请求幂等键),
            )
            连接.execute(
                "INSERT INTO platform_event VALUES (?, 'report_uploaded', 'analysis_job', ?, ?, ?)",
                (
                    随机编号("event"), 任务编号,
                    _JSON文本({"sha256": 摘要, "deduplication": {"blob": bool(内容), "report_version": 重复使用, "job": False}}), 时间,
                ),
            )
        return {
            "job_id": 任务编号,
            "run_id": 运行编号,
            "report_version_id": 报告版本编号,
            "sha256": 摘要,
            "deduplication": {"blob": bool(内容), "report_version": 重复使用, "job": False},
            "pdf_eof_ok": 尾标记正常,
        }

    def 解析报告文件(self, 报告版本编号: str) -> Path | None:
        with self.读连接() as 连接:
            行 = 连接.execute(
                """
                SELECT fl.root_code, fl.relative_path
                  FROM file_location fl
                 WHERE fl.report_version_id=? AND fl.is_available=1
                 ORDER BY CASE WHEN fl.root_code='UPLOADS' THEN 0 ELSE 1 END, fl.observed_at DESC
                 LIMIT 1
                """,
                (报告版本编号,),
            ).fetchone()
        if not 行:
            return None
        if 行["root_code"] == "UPLOADS":
            路径 = (self.路径.上传目录 / 行["relative_path"]).resolve()
            try:
                路径.relative_to(self.路径.上传目录)
            except ValueError:
                return None
            return 路径 if 路径.is_file() else None
        环境键 = f"ESG_REPORT_ROOT_{行['root_code']}"
        根目录值 = os.environ.get(环境键)
        if not 根目录值:
            return None
        根目录 = Path(根目录值).resolve()
        路径 = (根目录 / 行["relative_path"]).resolve()
        try:
            路径.relative_to(根目录)
        except ValueError:
            return None
        return 路径 if 路径.is_file() else None

    def 任务详情(self, 任务编号: str) -> dict[str, Any] | None:
        with self.读连接() as 连接:
            任务 = 连接.execute("SELECT * FROM analysis_job WHERE job_id=?", (任务编号,)).fetchone()
            if not 任务:
                return None
            统计 = 连接.execute(
                """
                SELECT COUNT(*) AS result_count,
                       SUM(candidate_status='candidate_found') AS candidate_count,
                       SUM(verification_status='needs_review') AS review_count
                  FROM extraction_result WHERE job_id=?
                """,
                (任务编号,),
            ).fetchone()
        返回 = dict(任务); 返回["result_summary"] = dict(统计); return 返回

    def 任务列表(self, 状态: str | None = None, 数量: int = 50) -> list[dict[str, Any]]:
        数量 = min(200, max(1, 数量))
        with self.读连接() as 连接:
            if 状态:
                行 = 连接.execute(
                    "SELECT * FROM analysis_job WHERE status=? ORDER BY created_at DESC LIMIT ?", (状态, 数量)
                ).fetchall()
            else:
                行 = 连接.execute("SELECT * FROM analysis_job ORDER BY created_at DESC LIMIT ?", (数量,)).fetchall()
        return [dict(x) for x in 行]

    def 结果列表(
        self, *, 任务编号: str | None = None, 报告版本编号: str | None = None,
        指标编号: str | None = None, 仅候选: bool = False,
    ) -> list[dict[str, Any]]:
        条件, 参数 = [], []
        for 列, 值 in (("er.job_id", 任务编号), ("er.report_version_id", 报告版本编号), ("er.indicator_id", 指标编号)):
            if 值: 条件.append(f"{列}=?"); 参数.append(值)
        if 仅候选: 条件.append("er.candidate_status='candidate_found'")
        where = "WHERE " + " AND ".join(条件) if 条件 else ""
        with self.读连接() as 连接:
            行 = 连接.execute(
                f"""
                SELECT er.*, ic.metric_name_cn, ic.dimension, ic.metric_type,
                       COUNT(es.evidence_id) AS evidence_count
                  FROM extraction_result er
                  JOIN indicator_catalog ic ON ic.indicator_id=er.indicator_id
                  LEFT JOIN evidence_span es ON es.result_id=er.result_id
                  {where}
                 GROUP BY er.result_id
                 ORDER BY er.report_year DESC, er.indicator_id, er.candidate_rank
                 LIMIT 5000
                """, 参数,
            ).fetchall()
        return [dict(x) for x in 行]

    def 证据列表(self, 结果编号: str) -> list[dict[str, Any]]:
        with self.读连接() as 连接:
            行 = 连接.execute(
                """
                SELECT evidence_id, result_id, report_version_id, page_no,
                       printed_page_label, source_text, evidence_type, bbox_json,
                       source_text_sha256, created_at
                  FROM evidence_span WHERE result_id=? ORDER BY page_no, evidence_id
                """, (结果编号,),
            ).fetchall()
        返回 = []
        for 项 in 行:
            数据 = dict(项); 数据["bbox"] = json.loads(数据.pop("bbox_json") or "null"); 返回.append(数据)
        return 返回

    def 证据详情(self, 证据编号: str) -> dict[str, Any] | None:
        with self.读连接() as 连接:
            行 = 连接.execute(
                """
                SELECT es.evidence_id, es.result_id, es.report_version_id, es.page_no,
                       es.printed_page_label, es.source_text, es.evidence_type,
                       es.bbox_json, es.source_text_sha256, es.created_at,
                       er.indicator_id, r.report_year, c.company_id, c.stock_code,
                       c.current_short_name
                  FROM evidence_span es
                  JOIN extraction_result er ON er.result_id=es.result_id
                  JOIN report_version rv ON rv.report_version_id=es.report_version_id
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN company c ON c.company_id=r.company_id
                 WHERE es.evidence_id=?
                """,
                (证据编号,),
            ).fetchone()
        if not 行:
            return None
        返回 = dict(行)
        返回["bbox"] = json.loads(返回.pop("bbox_json") or "null")
        return 返回

    def 趋势(self, 企业编号: str, 指标编号: str) -> dict[str, Any]:
        with self.读连接() as 连接:
            行 = 连接.execute(
                """
                WITH latest_success AS (
                    SELECT j.report_version_id, MAX(j.finished_at) AS finished_at
                      FROM analysis_job j
                     WHERE j.status IN ('succeeded','partial')
                     GROUP BY j.report_version_id
                )
                SELECT r.report_year, er.normalized_value, er.unit_normalized,
                       er.confidence, er.verification_status, er.result_id,
                       rv.report_version_id
                  FROM report r
                  JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                  JOIN latest_success ls ON ls.report_version_id=rv.report_version_id
                  JOIN analysis_job j ON j.report_version_id=rv.report_version_id
                                     AND j.finished_at=ls.finished_at
                  JOIN extraction_result er ON er.job_id=j.job_id
                  JOIN indicator_catalog ic ON ic.indicator_id=er.indicator_id
                 WHERE r.company_id=? AND er.indicator_id=?
                   AND ic.metric_type='quantitative'
                   AND er.candidate_status='candidate_found'
                   AND er.normalized_value IS NOT NULL
                   AND er.candidate_rank=1
                 ORDER BY r.report_year
                """,
                (企业编号, 指标编号),
            ).fetchall()
        单位 = {x["unit_normalized"] for x in 行}
        可比较 = len(行) >= 2 and len(单位) == 1 and None not in 单位
        return {
            "comparable": 可比较,
            "reason": None if 可比较 else "至少需要两个年份且标准化单位必须一致",
            "indicator_id": 指标编号,
            "company_id": 企业编号,
            "points": [dict(x) for x in 行] if 可比较 else [],
        }

    def 对比(self, 企业编号列表: list[str], 指标编号: str, 年份: int) -> dict[str, Any]:
        企业编号列表 = list(dict.fromkeys(企业编号列表))[:20]
        if not 企业编号列表:
            return {"comparable": False, "reason": "未选择企业", "items": []}
        占位符 = ",".join("?" for _ in 企业编号列表)
        with self.读连接() as 连接:
            行 = 连接.execute(
                f"""
                SELECT c.company_id, c.stock_code, c.current_short_name,
                       r.report_year, er.raw_value, er.normalized_value,
                       er.unit_normalized, er.confidence, er.verification_status,
                       er.result_id
                  FROM company c JOIN report r ON r.company_id=c.company_id
                  JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                  JOIN analysis_job j ON j.report_version_id=rv.report_version_id
                                    AND j.status IN ('succeeded','partial')
                  JOIN extraction_result er ON er.job_id=j.job_id
                 WHERE c.company_id IN ({占位符}) AND r.report_year=?
                   AND er.indicator_id=? AND er.candidate_rank=1
                   AND er.candidate_status='candidate_found'
                 ORDER BY c.stock_code, j.finished_at DESC
                """,
                [*企业编号列表, 年份, 指标编号],
            ).fetchall()
        去重: dict[str, dict[str, Any]] = {}
        for 项 in 行:
            去重.setdefault(项["company_id"], dict(项))
        单位 = {x.get("unit_normalized") for x in 去重.values()}
        可比较 = len(去重) >= 2 and len(单位) == 1 and None not in 单位
        return {
            "comparable": 可比较,
            "reason": None if 可比较 else "至少需要两家企业且标准化单位必须一致",
            "indicator_id": 指标编号, "year": 年份,
            "items": list(去重.values()) if 可比较 else [],
        }

    def 搜索(self, 关键词: str, 数量: int = 20) -> dict[str, Any]:
        关键词, 数量 = 关键词.strip(), min(100, max(1, 数量))
        if not 关键词:
            return {"companies": [], "reports": [], "indicators": []}
        模式 = f"%{关键词}%"
        with self.读连接() as 连接:
            企业 = [dict(x) for x in 连接.execute(
                "SELECT company_id,stock_code,current_short_name FROM company WHERE stock_code LIKE ? OR current_short_name LIKE ? OR legal_name LIKE ? ORDER BY stock_code LIMIT ?",
                (模式, 模式, 模式, 数量),
            )]
            报告 = [dict(x) for x in 连接.execute(
                """
                SELECT r.report_id,rv.report_version_id,r.report_year,r.canonical_title,c.stock_code,c.current_short_name
                  FROM report r JOIN company c ON c.company_id=r.company_id
                  JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                 WHERE r.canonical_title LIKE ? ORDER BY r.report_year DESC LIMIT ?
                """, (模式, 数量),
            )]
            指标 = [dict(x) for x in 连接.execute(
                "SELECT indicator_id,metric_name_cn,dimension,metric_type FROM indicator_catalog WHERE metric_name_cn LIKE ? OR definition LIKE ? OR aliases_json LIKE ? ORDER BY indicator_id LIMIT ?",
                (模式, 模式, 模式, 数量),
            )]
            证据 = [dict(x) for x in 连接.execute(
                """
                SELECT es.evidence_id, es.result_id, es.report_version_id, es.page_no,
                       substr(es.source_text, 1, 240) AS source_text_preview,
                       er.indicator_id, ic.metric_name_cn, c.stock_code,
                       c.current_short_name, r.report_year
                  FROM evidence_span es
                  JOIN extraction_result er ON er.result_id=es.result_id
                  JOIN indicator_catalog ic ON ic.indicator_id=er.indicator_id
                  JOIN report_version rv ON rv.report_version_id=es.report_version_id
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN company c ON c.company_id=r.company_id
                 WHERE es.source_text LIKE ?
                 ORDER BY es.created_at DESC LIMIT ?
                """,
                (模式, 数量),
            )]
        return {"companies": 企业, "reports": 报告, "indicators": 指标, "evidence": 证据}
