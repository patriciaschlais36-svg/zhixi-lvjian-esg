#!/usr/bin/env python3
"""Build a three-year ESG report manifest and import metadata into SQLite.

The tool deliberately does not parse PDF text. It only inspects filesystem
metadata, the PDF header/trailer, and optionally streams bytes for SHA-256.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


CONTRACT_VERSION = "three-year-report-metadata-v1"
YEARS = (2023, 2024, 2025)
TAG_ORDER = ("CSR", "ENV", "ESG", "SD", "OTHER")
PRIMARY_TAG_ORDER = ("ESG", "SD", "CSR", "ENV", "OTHER")
ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://data-element-contest.local/esg-three-year"
)
FILENAME_RE = re.compile(
    r"^(?P<stock_code>\d{6})_"
    r"(?P<report_year>202[345])_"
    r"(?P<tag_block>(?:#[A-Za-z0-9]+)+)_"
    r"(?P<short_name>[^_]+)_"
    r"(?P<report_title>.+)_"
    r"(?P<disclosure_date>\d{4}-\d{2}-\d{2})\.pdf$"
)
TAG_RE = re.compile(r"#([A-Za-z0-9]+)")
YEAR_IN_DIR_RE = re.compile(r"(?<!\d)(202[345])(?!\d)")

MANIFEST_COLUMNS = (
    "manifest_row_id",
    "exchange",
    "stock_code",
    "company_id",
    "report_year",
    "raw_tag_block",
    "report_type_key",
    "report_type_tags",
    "primary_report_type",
    "short_name_raw",
    "report_title_raw",
    "language_code",
    "scope_code",
    "edition_no",
    "source_site",
    "source_announcement_id",
    "source_url",
    "disclosure_date",
    "root_code",
    "relative_path",
    "original_file_name",
    "directory_year",
    "file_size_bytes",
    "modified_time_ns",
    "sha256",
    "hash_state",
    "pdf_header_ok",
    "pdf_eof_ok",
    "page_count",
    "is_encrypted",
    "logical_key",
    "report_id",
    "file_blob_id",
    "content_key",
    "report_version_id",
    "version_no",
    "is_current",
    "version_reason",
    "coverage_status",
    "verification_status",
    "quality_flags",
    "import_batch_id",
    "contract_version",
    "row_sha256",
)


class ManifestError(RuntimeError):
    """Base class for user-correctable manifest errors."""


class FilenameParseError(ManifestError):
    """Raised when one or more PDF names violate the strict contract."""


class IntegrityError(ManifestError):
    """Raised when SQLite integrity or idempotency checks fail."""


@dataclass(frozen=True)
class PriorityCompany:
    stock_code: str
    short_name: str
    source_rank: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{uuid.uuid5(ID_NAMESPACE, prefix + ':' + value).hex}"


def normalize_tags(raw_tag_block: str) -> tuple[list[str], str, str]:
    raw_tags = [match.group(1).upper() for match in TAG_RE.finditer(raw_tag_block)]
    if not raw_tags or "".join(f"#{tag}" for tag in raw_tags) != raw_tag_block.upper():
        raise FilenameParseError(f"非法标签块: {raw_tag_block}")
    mapped = {tag if tag in TAG_ORDER[:-1] else "OTHER" for tag in raw_tags}
    ordered = [tag for tag in TAG_ORDER if tag in mapped]
    primary = next(tag for tag in PRIMARY_TAG_ORDER if tag in mapped)
    return ordered, "+".join(ordered), primary


def parse_filename(path: Path) -> dict[str, Any]:
    match = FILENAME_RE.fullmatch(path.name)
    if not match:
        raise FilenameParseError(
            "文件名不符合严格格式 "
            "{代码}_{2023|2024|2025}_{#标签...}_{简称}_{标题}_{YYYY-MM-DD}.pdf: "
            f"{path.name}"
        )
    parsed = match.groupdict()
    try:
        date.fromisoformat(parsed["disclosure_date"])
    except ValueError as exc:
        raise FilenameParseError(
            f"文件名披露日期不是有效 ISO 日期: {path.name}"
        ) from exc
    tags, type_key, primary = normalize_tags(parsed["tag_block"])
    return {
        "stock_code": parsed["stock_code"],
        "report_year": int(parsed["report_year"]),
        "raw_tag_block": parsed["tag_block"],
        "report_type_tags": tags,
        "report_type_key": type_key,
        "primary_report_type": primary,
        "short_name_raw": parsed["short_name"],
        "report_title_raw": parsed["report_title"],
        "disclosure_date": parsed["disclosure_date"],
    }


def infer_directory_year(relative_path: Path) -> int | None:
    years: set[int] = set()
    for part in relative_path.parts[:-1]:
        years.update(int(value) for value in YEAR_IN_DIR_RE.findall(part))
    if len(years) > 1:
        raise FilenameParseError(
            f"相对路径包含多个年度目录，无法交叉校验: {relative_path.as_posix()}"
        )
    return next(iter(years), None)


def stream_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: Path, compute_hash: bool) -> dict[str, Any]:
    stat = path.stat()
    if stat.st_size <= 0:
        raise ManifestError(f"空文件不能入库: {path}")
    with path.open("rb") as handle:
        header = handle.read(5)
        handle.seek(max(0, stat.st_size - 4096))
        trailer = handle.read()
    return {
        "file_size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "pdf_header_ok": int(header == b"%PDF-"),
        "pdf_eof_ok": int(b"%%EOF" in trailer),
        "sha256": stream_sha256(path) if compute_hash else "",
        "hash_state": "computed" if compute_hash else "skipped",
        "page_count": None,
        "is_encrypted": None,
    }


def _pick_column(fieldnames: Sequence[str], choices: Sequence[str]) -> str | None:
    normalized = {name.strip().lower(): name for name in fieldnames if name}
    for choice in choices:
        if choice.lower() in normalized:
            return normalized[choice.lower()]
    return None


def read_priority_companies(csv_path: Path) -> list[PriorityCompany]:
    if not csv_path.is_file():
        raise ManifestError(f"priority_200 CSV 不存在: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ManifestError("priority_200 CSV 缺少表头")
        code_column = _pick_column(
            reader.fieldnames, ("stock_code", "股票代码", "证券代码")
        )
        name_column = _pick_column(
            reader.fieldnames, ("short_name", "简称", "公司简称", "证券简称")
        )
        if not code_column or not name_column:
            raise ManifestError(
                "priority_200 CSV 必须包含 stock_code/short_name（或对应中文列）"
            )
        companies: list[PriorityCompany] = []
        seen: set[str] = set()
        for rank, row in enumerate(reader, start=1):
            code = (row.get(code_column) or "").strip()
            short_name = (row.get(name_column) or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                raise ManifestError(f"priority_200 第 {rank} 行股票代码非法: {code!r}")
            if not short_name:
                raise ManifestError(f"priority_200 第 {rank} 行简称为空: {code}")
            if code in seen:
                raise ManifestError(f"priority_200 股票代码重复: {code}")
            seen.add(code)
            companies.append(PriorityCompany(code, short_name, rank))
    if not companies:
        raise ManifestError("priority_200 CSV 没有数据行")
    return companies


def discover_rows(
    report_root: Path,
    root_code: str,
    compute_hash: bool,
    language_code: str,
    scope_code: str,
    edition_no: int,
) -> list[dict[str, Any]]:
    if not report_root.is_dir():
        raise ManifestError(f"报告根目录不存在: {report_root}")
    pdf_paths = sorted(
        (path for path in report_root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda value: value.relative_to(report_root).as_posix(),
    )
    if not pdf_paths:
        raise ManifestError(f"报告根目录没有 PDF: {report_root}")

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in pdf_paths:
        relative = path.relative_to(report_root)
        try:
            parsed = parse_filename(path)
            directory_year = infer_directory_year(relative)
            if directory_year is not None and directory_year != parsed["report_year"]:
                raise FilenameParseError(
                    f"目录年度 {directory_year} 与文件名年度 {parsed['report_year']} 冲突: "
                    f"{relative.as_posix()}"
                )
            inspected = inspect_pdf(path, False)
        except ManifestError as exc:
            errors.append(str(exc))
            continue

        company_id = stable_id("company", f"SSE:{parsed['stock_code']}")
        logical_key = "|".join(
            (
                "SSE",
                parsed["stock_code"],
                str(parsed["report_year"]),
                parsed["report_type_key"],
                language_code,
                scope_code,
                str(edition_no),
            )
        )
        report_id = stable_id("report", logical_key)
        quality_flags: list[str] = []
        disclosure_year = int(parsed["disclosure_date"][:4])
        if disclosure_year > parsed["report_year"] + 1:
            quality_flags.append("delayed_disclosure")
        if inspected["file_size_bytes"] < 200 * 1024:
            quality_flags.append("small_file_review")
        if not inspected["pdf_header_ok"]:
            quality_flags.append("pdf_header_invalid")
        if not inspected["pdf_eof_ok"]:
            quality_flags.append("pdf_eof_missing")

        relative_posix = relative.as_posix()
        if inspected["sha256"]:
            content_key = f"sha256:{inspected['sha256']}"
        else:
            content_key = (
                f"metadata:{root_code}:{relative_posix}:"
                f"{inspected['file_size_bytes']}:{inspected['modified_time_ns']}"
            )
        file_blob_id = stable_id("blob", content_key)
        rows.append(
            {
                "exchange": "SSE",
                **parsed,
                "company_id": company_id,
                "language_code": language_code,
                "scope_code": scope_code,
                "edition_no": edition_no,
                "source_site": "",
                "source_announcement_id": "",
                "source_url": "",
                "root_code": root_code,
                "relative_path": relative_posix,
                "original_file_name": path.name,
                "directory_year": directory_year,
                **inspected,
                "logical_key": logical_key,
                "report_id": report_id,
                "file_blob_id": file_blob_id,
                "content_key": content_key,
                "quality_flags": quality_flags,
            }
        )
    if errors:
        preview = "\n".join(f"- {message}" for message in errors[:20])
        suffix = "" if len(errors) <= 20 else f"\n- ...另有 {len(errors) - 20} 个错误"
        raise FilenameParseError(
            f"严格文件名/目录校验失败，共 {len(errors)} 个文件：\n{preview}{suffix}"
        )
    return rows


def resolve_hash_scope(compute_hash: bool, hash_scope: str) -> str:
    normalized = hash_scope.strip().lower()
    allowed = {"none", "p200", "p531", "all"}
    if normalized not in allowed:
        raise ManifestError(
            f"哈希范围必须是 {', '.join(sorted(allowed))} 之一: {hash_scope!r}"
        )
    if compute_hash and normalized != "none":
        raise ManifestError("--计算哈希 与 --哈希范围 不能同时使用")
    return "all" if compute_hash else normalized


def select_hash_stock_codes(
    rows: list[dict[str, Any]],
    priority: Sequence[PriorityCompany] | None,
    hash_scope: str,
) -> set[str] | None:
    if hash_scope == "none":
        return set()
    if hash_scope == "all":
        return None
    if priority is None:
        raise ManifestError(f"哈希范围 {hash_scope} 需要 priority_200 企业清单")

    priority_codes = {company.stock_code for company in priority}
    if hash_scope == "p200":
        return priority_codes

    valid_years: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        if (
            row["stock_code"] in priority_codes
            and row["report_year"] in YEARS
            and row["pdf_header_ok"]
            and row["pdf_eof_ok"]
        ):
            valid_years[row["stock_code"]].add(row["report_year"])
    return {
        code for code, years in valid_years.items() if set(YEARS).issubset(years)
    }


def apply_scoped_hashes(
    rows: list[dict[str, Any]],
    report_root: Path,
    root_code: str,
    priority: Sequence[PriorityCompany] | None,
    hash_scope: str,
) -> None:
    selected_codes = select_hash_stock_codes(rows, priority, hash_scope)
    for row in rows:
        should_hash = selected_codes is None or row["stock_code"] in selected_codes
        if should_hash:
            row["sha256"] = stream_sha256(report_root / row["relative_path"])
            row["hash_state"] = "computed"
            content_key = f"sha256:{row['sha256']}"
        else:
            row["sha256"] = ""
            row["hash_state"] = "skipped"
            content_key = (
                f"metadata:{root_code}:{row['relative_path']}:"
                f"{row['file_size_bytes']}:{row['modified_time_ns']}"
            )
        row["content_key"] = content_key
        row["file_blob_id"] = stable_id("blob", content_key)


def assign_versions(rows: list[dict[str, Any]]) -> None:
    hash_logical_keys: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["sha256"]:
            hash_logical_keys[row["sha256"]].add(row["logical_key"])

    by_logical_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_logical_key[row["logical_key"]].append(row)

    for logical_key, report_rows in by_logical_key.items():
        by_content: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in report_rows:
            by_content[row["content_key"]].append(row)
        identities = sorted(
            by_content,
            key=lambda key: min(
                (row["disclosure_date"], row["relative_path"])
                for row in by_content[key]
            ),
        )
        accepted_identity = next(
            (
                key
                for key in identities
                if any(
                    row["pdf_header_ok"] and row["pdf_eof_ok"]
                    for row in by_content[key]
                )
            ),
            None,
        )
        for version_no, content_key in enumerate(identities, start=1):
            content_rows = by_content[content_key]
            first = min(content_rows, key=lambda row: row["relative_path"])
            hash_conflict = bool(
                first["sha256"]
                and len(hash_logical_keys[first["sha256"]]) > 1
            )
            valid_pdf = all(
                row["pdf_header_ok"] and row["pdf_eof_ok"]
                for row in content_rows
            )
            is_accepted = (
                content_key == accepted_identity and valid_pdf and not hash_conflict
            )
            flags = sorted(
                {
                    flag
                    for row in content_rows
                    for flag in row["quality_flags"]
                }
            )
            if hash_conflict:
                flags.append("hash_identity_conflict")
            report_version_id = stable_id(
                "version", f"{first['report_id']}:{content_key}"
            )
            for row in content_rows:
                row.update(
                    {
                        "report_version_id": report_version_id,
                        "version_no": version_no,
                        "is_current": int(is_accepted),
                        "version_reason": "initial" if version_no == 1 else "unknown",
                        "verification_status": "accepted" if is_accepted else "review",
                        "coverage_status": "present" if is_accepted else "pending_verification",
                        "quality_flags": flags,
                    }
                )


def finalize_manifest(
    rows: list[dict[str, Any]], root_code: str, contract_version: str
) -> tuple[list[dict[str, Any]], str, str]:
    rows.sort(
        key=lambda row: (
            row["stock_code"],
            row["report_year"],
            row["logical_key"],
            row["version_no"],
            row["relative_path"],
        )
    )
    batch_seed = [
        {
            "relative_path": row["relative_path"],
            "file_size_bytes": row["file_size_bytes"],
            "modified_time_ns": row["modified_time_ns"],
            "sha256": row["sha256"],
            "report_version_id": row["report_version_id"],
        }
        for row in rows
    ]
    import_batch_id = "batch_" + sha256_text(
        canonical_json(
            {
                "contract_version": contract_version,
                "source_root": f"root://{root_code}",
                "rows": batch_seed,
            }
        )
    )[:24]
    for index, row in enumerate(rows, start=1):
        row["manifest_row_id"] = f"M{index:06d}"
        row["import_batch_id"] = import_batch_id
        row["contract_version"] = contract_version
        row["row_sha256"] = sha256_text(canonical_json(row))
    manifest_sha256 = sha256_text(canonical_json(rows))
    return rows, import_batch_id, manifest_sha256


def build_manifest(
    report_root: Path,
    root_code: str,
    compute_hash: bool,
    language_code: str = "zh-CN",
    scope_code: str = "unknown",
    edition_no: int = 1,
    contract_version: str = CONTRACT_VERSION,
    priority: Sequence[PriorityCompany] | None = None,
    hash_scope: str = "none",
) -> tuple[list[dict[str, Any]], str, str]:
    effective_hash_scope = resolve_hash_scope(compute_hash, hash_scope)
    rows = discover_rows(
        report_root,
        root_code,
        False,
        language_code,
        scope_code,
        edition_no,
    )
    apply_scoped_hashes(
        rows, report_root, root_code, priority, effective_hash_scope
    )
    assign_versions(rows)
    return finalize_manifest(rows, root_code, contract_version)


def connect_database(sqlite_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise IntegrityError("SQLite foreign_keys 未能启用")
    return connection


def initialize_database(connection: sqlite3.Connection, schema_path: Path) -> None:
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    connection.execute("PRAGMA foreign_keys = ON")


def _insert_ignore(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
    added: dict[str, int],
) -> bool:
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    cursor = connection.execute(sql, tuple(values))
    inserted = cursor.rowcount == 1
    if inserted:
        added[table] += 1
    return inserted


def _alias_spans(rows: list[dict[str, Any]]) -> list[tuple[str, str, int, int]]:
    observations: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in rows:
        observations[(row["company_id"], row["short_name_raw"])].add(row["report_year"])
    spans: list[tuple[str, str, int, int]] = []
    for (company_id, alias), observed_years in sorted(observations.items()):
        years = sorted(observed_years)
        start = previous = years[0]
        for year in years[1:]:
            if year == previous + 1:
                previous = year
                continue
            spans.append((company_id, alias, start, previous))
            start = previous = year
        spans.append((company_id, alias, start, previous))
    return spans


def _canonical_slots(
    rows: list[dict[str, Any]], priority: list[PriorityCompany]
) -> dict[tuple[str, int], dict[str, Any] | None]:
    candidates: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["is_current"] and row["verification_status"] == "accepted":
            candidates[(row["stock_code"], row["report_year"])].append(row)
    slots: dict[tuple[str, int], dict[str, Any] | None] = {}
    for company in priority:
        for year in YEARS:
            values = candidates.get((company.stock_code, year), [])
            slots[(company.stock_code, year)] = min(
                values,
                key=lambda row: (
                    row["report_type_key"], row["report_id"], row["report_version_id"]
                ),
                default=None,
            )
    return slots


def apply_manifest(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    priority: list[PriorityCompany],
    report_root: Path,
    root_code: str,
    manifest_sha256: str,
    import_batch_id: str,
    contract_version: str,
    hash_scope: str,
    now: str,
) -> dict[str, int]:
    added: dict[str, int] = defaultdict(int)
    accepted_versions = {
        row["report_version_id"]
        for row in rows
        if row["verification_status"] == "accepted"
    }
    review_versions = {
        row["report_version_id"]
        for row in rows
        if row["verification_status"] != "accepted"
    }
    _insert_ignore(
        connection,
        "import_batch",
        (
            "import_batch_id", "source_root", "root_code", "manifest_sha256",
            "contract_version", "hash_mode", "status", "discovered_count",
            "accepted_count", "review_count", "started_at",
        ),
        (
            import_batch_id, f"root://{root_code}", root_code, manifest_sha256,
            contract_version, "computed" if hash_scope != "none" else "skipped", "running",
            len(rows), len(accepted_versions), len(review_versions), now,
        ),
        added,
    )

    priority_by_code = {item.stock_code: item for item in priority}
    rows_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_code[row["stock_code"]].append(row)
    all_codes = sorted(set(rows_by_code) | set(priority_by_code))
    for code in all_codes:
        company_id = stable_id("company", f"SSE:{code}")
        if code in priority_by_code:
            current_name = priority_by_code[code].short_name
        else:
            current_name = max(
                rows_by_code[code],
                key=lambda row: (row["report_year"], row["disclosure_date"]),
            )["short_name_raw"]
        _insert_ignore(
            connection,
            "company",
            (
                "company_id", "exchange", "stock_code", "current_short_name",
                "listing_status", "created_at", "updated_at",
            ),
            (company_id, "SSE", code, current_name, "unknown", now, now),
            added,
        )

    for company_id, alias, start, end in _alias_spans(rows):
        valid_from = f"{start}-01-01"
        valid_to = f"{end}-12-31"
        _insert_ignore(
            connection,
            "company_alias",
            ("alias_id", "company_id", "alias_name", "alias_type", "valid_from", "valid_to", "source"),
            (
                stable_id("alias", f"{company_id}:{alias}:{valid_from}"),
                company_id, alias, "short_name", valid_from, valid_to, "filename",
            ),
            added,
        )
    observed_aliases = {
        (row["stock_code"], row["short_name_raw"]) for row in rows
    }
    for company in priority:
        if (company.stock_code, company.short_name) in observed_aliases:
            continue
        company_id = stable_id("company", f"SSE:{company.stock_code}")
        _insert_ignore(
            connection,
            "company_alias",
            ("alias_id", "company_id", "alias_name", "alias_type", "valid_from", "valid_to", "source"),
            (
                stable_id("alias", f"{company_id}:{company.short_name}:priority"),
                company_id, company.short_name, "short_name", None, None, "priority_200",
            ),
            added,
        )

    first_by_report: dict[str, dict[str, Any]] = {}
    for row in rows:
        first_by_report.setdefault(row["report_id"], row)
    for report_id, row in sorted(first_by_report.items()):
        report_has_accepted = any(
            value["report_id"] == report_id
            and value["verification_status"] == "accepted"
            for value in rows
        )
        _insert_ignore(
            connection,
            "report",
            (
                "report_id", "company_id", "report_year", "report_type_key",
                "primary_report_type", "language_code", "scope_code", "edition_no",
                "source_site", "source_announcement_id", "canonical_title", "logical_key",
                "status", "created_at", "updated_at",
            ),
            (
                report_id, row["company_id"], row["report_year"], row["report_type_key"],
                row["primary_report_type"], row["language_code"], row["scope_code"],
                row["edition_no"], row["source_site"] or None,
                row["source_announcement_id"] or None, row["report_title_raw"],
                row["logical_key"], "active" if report_has_accepted else "review", now, now,
            ),
            added,
        )
        for tag in row["report_type_tags"]:
            _insert_ignore(
                connection,
                "report_type_tag",
                ("report_id", "type_code"),
                (report_id, tag),
                added,
            )

    first_by_blob: dict[str, dict[str, Any]] = {}
    for row in rows:
        first_by_blob.setdefault(row["file_blob_id"], row)
    for blob_id, row in sorted(first_by_blob.items()):
        _insert_ignore(
            connection,
            "file_blob",
            (
                "file_blob_id", "sha256", "file_size_bytes", "mime_type",
                "pdf_header_ok", "pdf_eof_ok", "hash_state", "modified_time_ns",
                "first_seen_at",
            ),
            (
                blob_id, row["sha256"] or None, row["file_size_bytes"],
                "application/pdf", row["pdf_header_ok"], row["pdf_eof_ok"],
                row["hash_state"], row["modified_time_ns"], now,
            ),
            added,
        )

    first_by_version: dict[str, dict[str, Any]] = {}
    for row in rows:
        first_by_version.setdefault(row["report_version_id"], row)
    for version_id, row in sorted(first_by_version.items()):
        _insert_ignore(
            connection,
            "report_version",
            (
                "report_version_id", "report_id", "file_blob_id", "version_no",
                "content_key", "disclosure_date", "source_url", "original_file_name",
                "short_name_raw", "is_current", "version_reason",
                "verification_status", "quality_flags_json", "created_at",
            ),
            (
                version_id, row["report_id"], row["file_blob_id"], row["version_no"],
                row["content_key"], row["disclosure_date"], row["source_url"] or None,
                row["original_file_name"], row["short_name_raw"], row["is_current"],
                row["version_reason"], row["verification_status"],
                canonical_json(row["quality_flags"]), now,
            ),
            added,
        )

    for row in rows:
        location_id = stable_id(
            "location", f"{row['root_code']}:{row['relative_path']}"
        )
        _insert_ignore(
            connection,
            "file_location",
            (
                "location_id", "file_blob_id", "report_version_id", "root_code",
                "relative_path", "observed_at", "is_available",
            ),
            (
                location_id, row["file_blob_id"], row["report_version_id"],
                row["root_code"], row["relative_path"], now, 1,
            ),
            added,
        )
        _insert_ignore(
            connection,
            "import_manifest_row",
            (
                "import_batch_id", "manifest_row_id", "report_version_id",
                "row_sha256", "row_json",
            ),
            (
                import_batch_id, row["manifest_row_id"], row["report_version_id"],
                row["row_sha256"], canonical_json(row),
            ),
            added,
        )

    canonical_slots = _canonical_slots(rows, priority)
    discovered_keys = {(row["stock_code"], row["report_year"]) for row in rows}
    for company in priority:
        company_id = stable_id("company", f"SSE:{company.stock_code}")
        for year in YEARS:
            canonical = canonical_slots[(company.stock_code, year)]
            if canonical:
                status = "present"
                reason = None
                report_id = canonical["report_id"]
                version_id = canonical["report_version_id"]
            elif (company.stock_code, year) in discovered_keys:
                status = "pending_verification"
                reason = "candidate_not_accepted"
                report_id = None
                version_id = None
            else:
                status = "missing_not_found"
                reason = "not_found_in_source_root"
                report_id = None
                version_id = None
            inserted = _insert_ignore(
                connection,
                "coverage_slot",
                (
                    "company_id", "report_year", "expected_in_scope", "coverage_status",
                    "canonical_report_id", "canonical_report_version_id", "reason_code",
                    "checked_at",
                ),
                (company_id, year, 1, status, report_id, version_id, reason, now),
                added,
            )
            if not inserted:
                connection.execute(
                    """
                    UPDATE coverage_slot
                    SET expected_in_scope = 1, coverage_status = ?,
                        canonical_report_id = ?, canonical_report_version_id = ?,
                        reason_code = ?, checked_at = ?
                    WHERE company_id = ? AND report_year = ?
                    """,
                    (status, report_id, version_id, reason, now, company_id, year),
                )

    complete_codes = [
        company.stock_code
        for company in priority
        if all(canonical_slots[(company.stock_code, year)] for year in YEARS)
    ]
    suffix = manifest_sha256[:12]
    snapshots = {
        "P200": (
            "mixed",
            "priority CSV 中的公司，以及这些公司 2025 年已验收当前报告版本",
        ),
        "P177": (
            "company",
            "P200 中 2023、2024、2025 三个覆盖槽位均为 present 的公司",
        ),
        "P531": (
            "report",
            "P177 公司三个年度覆盖槽位锁定的已验收当前报告版本",
        ),
    }
    snapshot_ids: dict[str, str] = {}
    for label, (kind, definition) in snapshots.items():
        code = f"{label}_{suffix}"
        snapshot_id = stable_id("snapshot", f"{label}:{manifest_sha256}")
        snapshot_ids[label] = snapshot_id
        _insert_ignore(
            connection,
            "dataset_snapshot",
            (
                "snapshot_id", "snapshot_code", "snapshot_label", "member_kind",
                "definition", "manifest_sha256", "frozen_at",
            ),
            (snapshot_id, code, label, kind, definition, manifest_sha256, now),
            added,
        )

    for company in priority:
        _insert_ignore(
            connection,
            "dataset_company_member",
            ("snapshot_id", "company_id", "member_rank"),
            (
                snapshot_ids["P200"],
                stable_id("company", f"SSE:{company.stock_code}"),
                company.source_rank,
            ),
            added,
        )
        report_2025 = canonical_slots[(company.stock_code, 2025)]
        if report_2025:
            _insert_ignore(
                connection,
                "dataset_member",
                ("snapshot_id", "report_version_id", "member_rank"),
                (
                    snapshot_ids["P200"], report_2025["report_version_id"],
                    company.source_rank,
                ),
                added,
            )

    for rank, code in enumerate(complete_codes, start=1):
        company_id = stable_id("company", f"SSE:{code}")
        _insert_ignore(
            connection,
            "dataset_company_member",
            ("snapshot_id", "company_id", "member_rank"),
            (snapshot_ids["P177"], company_id, rank),
            added,
        )
        for offset, year in enumerate(YEARS):
            row = canonical_slots[(code, year)]
            assert row is not None
            _insert_ignore(
                connection,
                "dataset_member",
                ("snapshot_id", "report_version_id", "member_rank"),
                (snapshot_ids["P531"], row["report_version_id"], (rank - 1) * 3 + offset + 1),
                added,
            )
    return dict(added)


def assert_database_integrity(connection: sqlite3.Connection) -> None:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise IntegrityError(
            f"foreign_key_check 失败，共 {len(foreign_key_errors)} 行"
        )
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    integrity_messages = [row[0] for row in integrity_rows]
    if integrity_messages != ["ok"]:
        raise IntegrityError(f"integrity_check 失败: {integrity_messages}")


def import_twice_and_verify(
    sqlite_path: Path,
    schema_path: Path,
    rows: list[dict[str, Any]],
    priority: list[PriorityCompany],
    report_root: Path,
    root_code: str,
    manifest_sha256: str,
    import_batch_id: str,
    contract_version: str,
    hash_scope: str,
) -> tuple[dict[str, int], dict[str, int]]:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(sqlite_path)
    now = utc_now()
    try:
        initialize_database(connection, schema_path)
        connection.execute("BEGIN IMMEDIATE")
        try:
            first_added = apply_manifest(
                connection, rows, priority, report_root, root_code,
                manifest_sha256, import_batch_id, contract_version, hash_scope, now,
            )
            assert_database_integrity(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        connection.execute("BEGIN IMMEDIATE")
        try:
            second_added = apply_manifest(
                connection, rows, priority, report_root, root_code,
                manifest_sha256, import_batch_id, contract_version, hash_scope, now,
            )
            nonzero = {name: count for name, count in second_added.items() if count}
            if nonzero:
                raise IntegrityError(f"二次导入出现新增实体: {nonzero}")
            connection.execute(
                """
                UPDATE import_batch
                SET status = 'succeeded', idempotency_verified = 1, finished_at = ?
                WHERE import_batch_id = ?
                """,
                (utc_now(), import_batch_id),
            )
            assert_database_integrity(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return first_added, second_added
    except Exception:
        try:
            connection.execute(
                "UPDATE import_batch SET status = 'failed', finished_at = ? "
                "WHERE import_batch_id = ?",
                (utc_now(), import_batch_id),
            )
            connection.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _atomic_write(path: Path, writer: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        writer(temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_manifest_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    def writer(temporary_path: Path) -> None:
        with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
            csv_writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            csv_writer.writeheader()
            for row in rows:
                output = {column: row.get(column) for column in MANIFEST_COLUMNS}
                for key, value in output.items():
                    if value is None:
                        output[key] = ""
                    elif isinstance(value, (list, dict)):
                        output[key] = canonical_json(value)
                csv_writer.writerow(output)
    _atomic_write(path, writer)


def write_manifest_json(
    path: Path, rows: list[dict[str, Any]], manifest_sha256: str
) -> None:
    payload = {
        "manifest_sha256": manifest_sha256,
        "row_count": len(rows),
        "rows": rows,
    }

    def writer(temporary_path: Path) -> None:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _atomic_write(path, writer)


def database_summary(sqlite_path: Path, manifest_sha256: str) -> dict[str, Any]:
    connection = connect_database(sqlite_path)
    try:
        slot_counts = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT coverage_status, COUNT(*) FROM coverage_slot GROUP BY coverage_status"
            )
        }
        snapshot_counts: dict[str, dict[str, int]] = {}
        for label in ("P200", "P177", "P531"):
            row = connection.execute(
                "SELECT snapshot_id, snapshot_code FROM dataset_snapshot "
                "WHERE snapshot_label = ? AND manifest_sha256 = ?",
                (label, manifest_sha256),
            ).fetchone()
            if row is None:
                continue
            snapshot_counts[label] = {
                "company_members": connection.execute(
                    "SELECT COUNT(*) FROM dataset_company_member WHERE snapshot_id = ?",
                    (row[0],),
                ).fetchone()[0],
                "report_members": connection.execute(
                    "SELECT COUNT(*) FROM dataset_member WHERE snapshot_id = ?",
                    (row[0],),
                ).fetchone()[0],
                "snapshot_code": row[1],
            }
        return {
            "company_count": connection.execute("SELECT COUNT(*) FROM company").fetchone()[0],
            "report_count": connection.execute("SELECT COUNT(*) FROM report").fetchone()[0],
            "report_version_count": connection.execute(
                "SELECT COUNT(*) FROM report_version"
            ).fetchone()[0],
            "accepted_report_version_count": connection.execute(
                "SELECT COUNT(*) FROM report_version WHERE verification_status = 'accepted'"
            ).fetchone()[0],
            "review_report_version_count": connection.execute(
                "SELECT COUNT(*) FROM report_version WHERE verification_status = 'review'"
            ).fetchone()[0],
            "active_report_count": connection.execute(
                "SELECT COUNT(*) FROM report WHERE status = 'active'"
            ).fetchone()[0],
            "review_report_count": connection.execute(
                "SELECT COUNT(*) FROM report WHERE status = 'review'"
            ).fetchone()[0],
            "coverage_slots": slot_counts,
            "snapshots": snapshot_counts,
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_error_count": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    finally:
        connection.close()


def run_import(
    report_root: Path,
    priority_csv: Path,
    sqlite_output: Path,
    manifest_csv: Path,
    manifest_json: Path,
    compute_hash: bool = False,
    hash_scope: str = "none",
    root_code: str = "REPORT_ROOT",
    language_code: str = "zh-CN",
    scope_code: str = "unknown",
    edition_no: int = 1,
    contract_version: str = CONTRACT_VERSION,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    effective_hash_scope = resolve_hash_scope(compute_hash, hash_scope)
    priority = read_priority_companies(priority_csv)
    rows, import_batch_id, manifest_sha256 = build_manifest(
        report_root=report_root,
        root_code=root_code,
        compute_hash=False,
        language_code=language_code,
        scope_code=scope_code,
        edition_no=edition_no,
        contract_version=contract_version,
        priority=priority,
        hash_scope=effective_hash_scope,
    )
    schema_path = Path(__file__).with_name("数据库结构.sql")
    if not schema_path.is_file():
        raise ManifestError(f"数据库结构文件不存在: {schema_path}")
    first_added, second_added = import_twice_and_verify(
        sqlite_path=sqlite_output,
        schema_path=schema_path,
        rows=rows,
        priority=priority,
        report_root=report_root,
        root_code=root_code,
        manifest_sha256=manifest_sha256,
        import_batch_id=import_batch_id,
        contract_version=contract_version,
        hash_scope=effective_hash_scope,
    )
    write_manifest_csv(manifest_csv, rows)
    write_manifest_json(manifest_json, rows, manifest_sha256)
    summary = database_summary(sqlite_output, manifest_sha256)
    hashed_rows = [row for row in rows if row["hash_state"] == "computed"]
    hash_year_counts = {
        str(year): sum(row["report_year"] == year for row in hashed_rows)
        for year in YEARS
    }
    summary.update(
        {
            "import_batch_id": import_batch_id,
            "manifest_sha256": manifest_sha256,
            "manifest_row_count": len(rows),
            "priority_company_count": len(priority),
            "hash_mode": "computed" if effective_hash_scope != "none" else "skipped",
            "hash_scope": effective_hash_scope,
            "hashed_report_count": len(hashed_rows),
            "hashed_company_count": len({row["stock_code"] for row in hashed_rows}),
            "hashed_bytes": sum(row["file_size_bytes"] for row in hashed_rows),
            "hash_year_counts": hash_year_counts,
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "first_pass_added": first_added,
            "second_pass_added": second_added,
            "sqlite_output": str(sqlite_output.resolve()),
            "manifest_csv": str(manifest_csv.resolve()),
            "manifest_json": str(manifest_json.resolve()),
        }
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="严格构建 2023-2025 报告 manifest，并幂等导入 SQLite（不解析 PDF 正文）"
    )
    parser.add_argument("--报告根目录", "--report-root", dest="report_root", required=True)
    parser.add_argument("--priority-200", dest="priority_csv", required=True)
    parser.add_argument("--SQLite输出", "--sqlite-output", dest="sqlite_output", required=True)
    parser.add_argument("--manifest-csv", required=True)
    parser.add_argument("--manifest-json", required=True)
    parser.add_argument(
        "--计算哈希", "--compute-hash", dest="compute_hash", action="store_true",
        help="兼容旧参数：流式计算全部 PDF 的 SHA-256",
    )
    parser.add_argument(
        "--哈希范围", "--hash-scope", dest="hash_scope",
        choices=("none", "p200", "p531", "all"), default="none",
        help="哈希范围：none（默认）、p200、p531（三年完整面板）或 all",
    )
    parser.add_argument("--根目录代码", "--root-code", dest="root_code", default="REPORT_ROOT")
    parser.add_argument("--语言", "--language-code", dest="language_code", default="zh-CN")
    parser.add_argument("--范围", "--scope-code", dest="scope_code", default="unknown")
    parser.add_argument("--版次", "--edition-no", dest="edition_no", type=int, default=1)
    parser.add_argument("--契约版本", "--contract-version", dest="contract_version", default=CONTRACT_VERSION)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.edition_no < 1:
        parser.error("--版次/--edition-no 必须大于等于 1")
    if args.compute_hash and args.hash_scope != "none":
        parser.error("--计算哈希 与 --哈希范围 不能同时使用")
    try:
        summary = run_import(
            report_root=Path(args.report_root),
            priority_csv=Path(args.priority_csv),
            sqlite_output=Path(args.sqlite_output),
            manifest_csv=Path(args.manifest_csv),
            manifest_json=Path(args.manifest_json),
            compute_hash=args.compute_hash,
            hash_scope=args.hash_scope,
            root_code=args.root_code,
            language_code=args.language_code,
            scope_code=args.scope_code,
            edition_no=args.edition_no,
            contract_version=args.contract_version,
        )
    except (ManifestError, OSError, sqlite3.Error) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
