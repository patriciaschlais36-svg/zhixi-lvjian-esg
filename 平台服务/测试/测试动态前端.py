from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


项目根目录 = Path(__file__).resolve().parents[2]
证据目录 = 项目根目录 / "测试证据" / "平台界面验收"
证据目录.mkdir(parents=True, exist_ok=True)
服务地址 = os.environ.get("ESG_TEST_BASE_URL", "http://127.0.0.1:8766")


def 画布非空(页面, 画布编号: str) -> dict[str, int | bool]:
    return 页面.locator(f"#{画布编号}").evaluate(
        """canvas => {
          const context = canvas.getContext('2d');
          const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
          let colored = 0;
          for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] > 0 && (pixels[i] < 248 || pixels[i + 1] < 248 || pixels[i + 2] < 248)) colored++;
          }
          return {width: canvas.width, height: canvas.height, colored, nonblank: colored > 100};
        }"""
    )


def main() -> None:
    结果: dict[str, object] = {"base_url": 服务地址, "views": {}, "screenshots": {}, "console_errors": []}
    with sync_playwright() as p:
        浏览器 = p.chromium.launch(headless=True)
        页面 = 浏览器.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        页面.on("console", lambda 消息: 结果["console_errors"].append(消息.text) if 消息.type == "error" else None)
        页面.on("pageerror", lambda 异常: 结果["console_errors"].append(str(异常)))
        页面.goto(服务地址, wait_until="networkidle", timeout=60_000)
        页面.wait_for_selector("#summaryKpis .kpi-item", timeout=30_000)

        结果["title"] = 页面.title()
        结果["service_status"] = 页面.locator("#serviceStatus").inner_text()
        结果["kpi_count"] = 页面.locator("#summaryKpis .kpi-item").count()
        结果["canvases"] = {
            编号: 画布非空(页面, 编号) for 编号 in ("yearChart", "jobChart", "indicatorChart")
        }
        桌面截图 = 证据目录 / "桌面端_分析总览.png"
        页面.screenshot(path=str(桌面截图), full_page=True)
        结果["screenshots"]["desktop"] = str(桌面截图.relative_to(项目根目录))

        for 视图 in ("upload", "companies", "indicators", "analysis", "evidence", "pipeline", "overview"):
            页面.locator(f'[data-view="{视图}"]').click()
            页面.wait_for_timeout(500)
            区域 = 页面.locator(f"#{视图}")
            结果["views"][视图] = {
                "visible": 区域.is_visible(),
                "active_nav": 页面.locator(f'[data-view="{视图}"]').get_attribute("aria-current"),
            }

        页面.locator('[data-view="companies"]').click()
        页面.wait_for_selector("#companyTable tbody tr", timeout=30_000)
        结果["company_rows"] = 页面.locator("#companyTable tbody tr").count()
        页面.locator("#companyTable [data-company-id]").first.click()
        页面.wait_for_selector("#companyDetail .report-timeline", timeout=10_000)
        结果["company_detail_reports"] = 页面.locator("#companyDetail .report-timeline article").count()

        页面.locator('[data-view="indicators"]').click()
        页面.wait_for_selector("#indicatorTable tbody tr", timeout=15_000)
        结果["indicator_rows"] = 页面.locator("#indicatorTable tbody tr").count()
        页面.locator("#indicatorDimension").select_option("E")
        结果["environment_indicator_rows"] = 页面.locator("#indicatorTable tbody tr").count()

        页面.locator('[data-view="analysis"]').click()
        企业一 = 页面.locator("#analysisCompany option", has_text="600000").first.get_attribute("value")
        企业二 = 页面.locator("#analysisCompany option", has_text="600004").first.get_attribute("value")
        页面.locator("#analysisCompany").select_option(企业一)
        页面.locator("#analysisIndicator").select_option("E_Q_009")
        页面.wait_for_function("() => document.querySelector('#trendMeta').textContent.includes('个年度')", timeout=15_000)
        结果["trend_meta"] = 页面.locator("#trendMeta").inner_text()
        结果["trend_canvas"] = 画布非空(页面, "trendChart")
        页面.locator("#analysisIndicator").select_option("E_Q_009")
        页面.locator("#analysisYear").select_option("2025")
        页面.locator("#compareCompanies").select_option([企业一, 企业二])
        页面.locator("#compareBtn").click()
        页面.wait_for_selector("#compareResult .comparison-bars article", timeout=15_000)
        结果["comparison_items"] = 页面.locator("#compareResult .comparison-bars article").count()
        分析截图 = 证据目录 / "桌面端_趋势与企业对比.png"
        页面.screenshot(path=str(分析截图), full_page=True)
        结果["screenshots"]["analysis"] = str(分析截图.relative_to(项目根目录))

        页面.locator('[data-view="evidence"]').click()
        页面.locator("#evidenceSearch").fill("温室气体")
        页面.locator("#evidenceSearchBtn").click()
        页面.wait_for_selector("#evidenceResults [data-open-evidence]", timeout=15_000)
        结果["evidence_search_items"] = 页面.locator("#evidenceResults [data-open-evidence]").count()
        页面.locator("#evidenceResults [data-open-evidence]").first.click()
        页面.wait_for_selector("#evidenceResults .evidence-detail", timeout=15_000)
        结果["evidence_detail_visible"] = 页面.locator("#evidenceResults .evidence-detail").is_visible()

        页面.locator('[data-view="pipeline"]').click()
        页面.wait_for_selector("#jobTable tbody tr", timeout=15_000)
        结果["job_rows"] = 页面.locator("#jobTable tbody tr").count()

        页面.locator("#globalSearch").fill("600")
        页面.wait_for_selector("#searchResults:not([hidden]) .search-group", timeout=15_000)
        结果["global_search_groups"] = 页面.locator("#searchResults .search-group").count()
        页面.locator("#searchClear").click()
        结果["global_search_closed"] = 页面.locator("#searchResults").is_hidden()

        页面.locator('[data-view="upload"]').click()
        结果["upload_form_controls"] = 页面.locator("#uploadForm input, #uploadForm button").count()
        页面.screenshot(path=str(证据目录 / "桌面端_报告解析.png"), full_page=True)

        移动页 = 浏览器.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        移动错误: list[str] = []
        移动页.on("console", lambda 消息: 移动错误.append(消息.text) if 消息.type == "error" else None)
        移动页.on("pageerror", lambda 异常: 移动错误.append(str(异常)))
        移动页.goto(服务地址, wait_until="networkidle", timeout=60_000)
        移动页.wait_for_selector("#summaryKpis .kpi-item", timeout=30_000)
        移动截图 = 证据目录 / "移动端_分析总览.png"
        移动页.screenshot(path=str(移动截图), full_page=True)
        结果["screenshots"]["mobile"] = str(移动截图.relative_to(项目根目录))
        结果["mobile_overflow"] = 移动页.evaluate(
            "() => ({viewport: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
        )
        结果["mobile_overflow_elements"] = 移动页.evaluate(
            """() => [...document.querySelectorAll('body *')].map(element => {
              const rect = element.getBoundingClientRect();
              return {tag: element.tagName, id: element.id, cls: element.className, left: rect.left, right: rect.right, width: rect.width};
            }).filter(item => item.right > document.documentElement.clientWidth + 2 || item.left < -2).slice(0, 30)"""
        )
        结果["console_errors"].extend(移动错误)
        移动页.close()
        浏览器.close()

    输出路径 = 证据目录 / "动态前端验收结果.json"
    输出路径.write_text(json.dumps(结果, ensure_ascii=False, indent=2), encoding="utf-8")

    assert 结果["title"].startswith("智析绿鉴")
    assert "服务就绪" in 结果["service_status"]
    assert 结果["kpi_count"] == 4
    assert all(项["nonblank"] for 项 in 结果["canvases"].values())
    assert all(项["visible"] and 项["active_nav"] == "page" for 项 in 结果["views"].values())
    assert 结果["company_rows"] > 0
    assert 结果["company_detail_reports"] > 0
    assert 结果["indicator_rows"] == 80
    assert 结果["environment_indicator_rows"] > 0
    assert "3 个年度" in 结果["trend_meta"]
    assert 结果["trend_canvas"]["nonblank"] is True
    assert 结果["comparison_items"] == 2
    assert 结果["evidence_search_items"] > 0
    assert 结果["evidence_detail_visible"] is True
    assert 结果["job_rows"] == 15
    assert 结果["global_search_groups"] > 0
    assert 结果["global_search_closed"] is True
    assert 结果["upload_form_controls"] >= 5
    assert 结果["mobile_overflow"]["scroll"] <= 结果["mobile_overflow"]["viewport"] + 2
    assert not 结果["console_errors"], 结果["console_errors"]

    print(json.dumps(结果, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
