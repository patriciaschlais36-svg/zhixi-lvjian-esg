from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("构建三年报告清单.py")
SPEC = importlib.util.spec_from_file_location("three_year_manifest", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_priority_csv(path: Path, companies: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("stock_code", "short_name", "target_year", "priority"))
        for code, name in companies:
            writer.writerow((code, name, "2025", "100"))


def write_fake_pdf(path: Path, marker: str = "sample") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        + marker.encode("utf-8")
        + b"\n%%EOF\n"
    )


class ThreeYearManifestTests(unittest.TestCase):
    def test_minimal_panel_aliases_snapshots_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            reports = base / "reports"
            priority_csv = base / "priority.csv"
            sqlite_output = base / "output.sqlite"
            manifest_csv = base / "manifest.csv"
            manifest_json = base / "manifest.json"
            write_priority_csv(
                priority_csv,
                [("600001", "甲公司新简称"), ("600002", "乙公司")],
            )
            write_fake_pdf(
                reports / "2023ESG报告原件" / "2023" /
                "600001_2023_#CSR#ESG_甲公司_社会责任暨ESG报告_2024-03-01.pdf"
            )
            write_fake_pdf(
                reports / "2024ESG报告原件" / "2024" /
                "600001_2024_#ESG_甲公司新简称_ESG报告_2025-03-01.pdf"
            )
            write_fake_pdf(
                reports / "600001_2025_#SD_甲公司新简称_可持续发展报告_2026-03-01.pdf"
            )
            write_fake_pdf(
                reports / "600002_2025_#ESG_乙公司_ESG报告_2026-04-01.pdf"
            )

            summary = MODULE.run_import(
                report_root=reports,
                priority_csv=priority_csv,
                sqlite_output=sqlite_output,
                manifest_csv=manifest_csv,
                manifest_json=manifest_json,
            )

            self.assertEqual(summary["manifest_row_count"], 4)
            self.assertEqual(summary["hash_mode"], "skipped")
            self.assertFalse(any(summary["second_pass_added"].values()))
            self.assertEqual(summary["integrity_check"], "ok")
            self.assertEqual(summary["foreign_key_error_count"], 0)
            self.assertEqual(summary["coverage_slots"]["present"], 4)
            self.assertEqual(summary["coverage_slots"]["missing_not_found"], 2)
            self.assertEqual(summary["snapshots"]["P200"]["company_members"], 2)
            self.assertEqual(summary["snapshots"]["P200"]["report_members"], 2)
            self.assertEqual(summary["snapshots"]["P177"]["company_members"], 1)
            self.assertEqual(summary["snapshots"]["P531"]["report_members"], 3)

            payload = json.loads(manifest_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["row_count"], 4)
            self.assertTrue(all(row["sha256"] == "" for row in payload["rows"]))
            multi_tag = next(
                row for row in payload["rows"] if row["raw_tag_block"] == "#CSR#ESG"
            )
            self.assertEqual(multi_tag["report_type_key"], "CSR+ESG")
            self.assertEqual(multi_tag["report_type_tags"], ["CSR", "ESG"])

            connection = MODULE.connect_database(sqlite_output)
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM company").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM report").fetchone()[0], 4)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM report_version").fetchone()[0], 4
                )
                aliases = {
                    row[0]
                    for row in connection.execute(
                        "SELECT alias_name FROM company_alias "
                        "WHERE company_id = ?",
                        (MODULE.stable_id("company", "SSE:600001"),),
                    )
                }
                self.assertEqual(aliases, {"甲公司", "甲公司新简称"})
                self.assertEqual(
                    connection.execute(
                        "SELECT idempotency_verified FROM import_batch"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

            repeated = MODULE.run_import(
                report_root=reports,
                priority_csv=priority_csv,
                sqlite_output=sqlite_output,
                manifest_csv=manifest_csv,
                manifest_json=manifest_json,
            )
            self.assertFalse(any(repeated["first_pass_added"].values()))
            self.assertFalse(any(repeated["second_pass_added"].values()))

    def test_hash_switch_computes_real_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            reports = base / "reports"
            priority_csv = base / "priority.csv"
            pdf_path = reports / "600003_2025_#ENV_丙公司_环境报告_2026-02-01.pdf"
            write_priority_csv(priority_csv, [("600003", "丙公司")])
            write_fake_pdf(pdf_path, "hash-me")

            summary = MODULE.run_import(
                report_root=reports,
                priority_csv=priority_csv,
                sqlite_output=base / "hash.sqlite",
                manifest_csv=base / "hash.csv",
                manifest_json=base / "hash.json",
                compute_hash=True,
            )
            payload = json.loads((base / "hash.json").read_text(encoding="utf-8"))
            expected = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            self.assertEqual(summary["hash_mode"], "computed")
            self.assertEqual(payload["rows"][0]["sha256"], expected)
            self.assertRegex(expected, r"^[0-9a-f]{64}$")

    def test_p531_hash_scope_only_hashes_complete_three_year_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            reports = base / "reports"
            priority_csv = base / "priority.csv"
            write_priority_csv(
                priority_csv,
                [("600010", "完整公司"), ("600011", "单年公司")],
            )
            for year in (2023, 2024, 2025):
                write_fake_pdf(
                    reports /
                    f"600010_{year}_#ESG_完整公司_ESG报告_{year + 1}-03-01.pdf",
                    f"complete-{year}",
                )
            write_fake_pdf(
                reports / "600011_2025_#ESG_单年公司_ESG报告_2026-03-01.pdf",
                "single-year",
            )

            summary = MODULE.run_import(
                report_root=reports,
                priority_csv=priority_csv,
                sqlite_output=base / "p531.sqlite",
                manifest_csv=base / "p531.csv",
                manifest_json=base / "p531.json",
                hash_scope="p531",
            )
            payload = json.loads((base / "p531.json").read_text(encoding="utf-8"))
            hashed = [row for row in payload["rows"] if row["hash_state"] == "computed"]
            skipped = [row for row in payload["rows"] if row["hash_state"] == "skipped"]
            self.assertEqual(summary["hash_scope"], "p531")
            self.assertEqual(summary["hashed_report_count"], 3)
            self.assertEqual(summary["hashed_company_count"], 1)
            self.assertEqual(summary["hash_year_counts"], {"2023": 1, "2024": 1, "2025": 1})
            self.assertEqual({row["stock_code"] for row in hashed}, {"600010"})
            self.assertEqual({row["stock_code"] for row in skipped}, {"600011"})

    def test_strict_filename_rejects_invalid_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            reports = Path(temporary_directory) / "reports"
            write_fake_pdf(reports / "600001_2022_#ESG_甲公司_报告_2023-01-01.pdf")
            with self.assertRaises(MODULE.FilenameParseError):
                MODULE.build_manifest(reports, "TEST_ROOT", False)

    def test_same_logical_report_keeps_distinct_file_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            reports = base / "reports"
            priority_csv = base / "priority.csv"
            name = "600004_2025_#ESG_丁公司_ESG报告_2026-03-01.pdf"
            write_priority_csv(priority_csv, [("600004", "丁公司")])
            write_fake_pdf(reports / "copy_a" / name, "first bytes")
            write_fake_pdf(reports / "copy_b" / name, "changed bytes")

            MODULE.run_import(
                report_root=reports,
                priority_csv=priority_csv,
                sqlite_output=base / "versions.sqlite",
                manifest_csv=base / "versions.csv",
                manifest_json=base / "versions.json",
            )
            connection = sqlite3.connect(base / "versions.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM report").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM report_version").fetchone()[0], 2
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM report_version WHERE is_current = 1"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM report_version WHERE verification_status = 'review'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
