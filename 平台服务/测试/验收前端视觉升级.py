from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


项目根目录 = Path(__file__).resolve().parents[2]
证据目录 = 项目根目录 / "测试证据" / "界面视觉升级验收"
证据目录.mkdir(parents=True, exist_ok=True)
服务地址 = os.environ.get("ESG_TEST_BASE_URL", "http://127.0.0.1:8000")
浏览器路径 = os.environ.get("ESG_PLAYWRIGHT_CHROMIUM")


def 画布状态(页面, 画布编号: str) -> dict[str, int | bool]:
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
    结果: dict[str, object] = {
        "base_url": 服务地址,
        "landing": {},
        "desktop": {},
        "mobile": {},
        "views": {},
        "screenshots": {},
        "console_errors": [],
    }

    with sync_playwright() as p:
        启动参数 = {"headless": True}
        if 浏览器路径:
            启动参数["executable_path"] = 浏览器路径
        浏览器 = p.chromium.launch(**启动参数)

        页面 = 浏览器.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        页面.on("console", lambda 消息: 结果["console_errors"].append(消息.text) if 消息.type == "error" else None)
        页面.on("pageerror", lambda 异常: 结果["console_errors"].append(str(异常)))
        页面.goto(服务地址, wait_until="networkidle", timeout=60_000)
        页面.wait_for_selector("#siteHeroTitle", timeout=30_000)
        页面.wait_for_selector("#summaryKpis .kpi-item", timeout=30_000)

        结果["landing"] = {
            "heading": 页面.locator("#siteHeroTitle").inner_text(),
            "hero_height": 页面.locator("#siteHero").evaluate("element => Math.round(element.getBoundingClientRect().height)"),
            "viewport_height": 页面.evaluate("window.innerHeight"),
            "desktop_nav_links": 页面.locator(".site-hero-links [data-landing-view]").count(),
            "report_sheets": 页面.locator(".site-hero-sheet").count(),
            "video": 页面.locator(".site-hero-video").evaluate(
                """video => ({
                  autoplay: video.autoplay,
                  muted: video.muted,
                  loop: video.loop,
                  playsInline: video.playsInline,
                  poster: video.getAttribute('poster'),
                  source: video.querySelector('source')?.getAttribute('src') || ''
                })"""
            ),
        }

        结果["desktop"] = {
            "title": 页面.title(),
            "hero_visible": 页面.locator(".overview-hero").is_visible(),
            "hero_heading": 页面.locator(".overview-hero h2").inner_text(),
            "kpi_count": 页面.locator("#summaryKpis .kpi-item").count(),
            "panel_count": 页面.locator("#overview .dashboard-grid > .panel").count(),
            "service_status": 页面.locator("#serviceStatus").inner_text(),
            "canvases": {编号: 画布状态(页面, 编号) for 编号 in ("yearChart", "jobChart", "indicatorChart")},
            "document_overflow": 页面.evaluate("() => ({viewport: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"),
        }
        页面.evaluate("window.scrollTo(0, 0)")
        桌面截图 = 证据目录 / "桌面端_新版分析总览.png"
        页面.screenshot(path=str(桌面截图), full_page=False)
        结果["screenshots"]["desktop_overview"] = str(桌面截图.relative_to(项目根目录))
        全页截图 = 证据目录 / "桌面端_新版分析总览_全页审计.png"
        页面.screenshot(path=str(全页截图), full_page=True)
        结果["screenshots"]["desktop_overview_full"] = str(全页截图.relative_to(项目根目录))

        页面.locator(".site-hero-primary").click()
        页面.wait_for_timeout(900)
        结果["landing"]["cta_workspace_top"] = 页面.locator(".app-shell").evaluate("element => Math.round(element.getBoundingClientRect().top)")

        for 视图 in ("upload", "companies", "indicators", "analysis", "evidence", "pipeline", "overview"):
            页面.locator(f'[data-view="{视图}"]').click()
            页面.wait_for_timeout(300)
            结果["views"][视图] = {
                "visible": 页面.locator(f"#{视图}").is_visible(),
                "active_nav": 页面.locator(f'[data-view="{视图}"]').get_attribute("aria-current"),
            }

        页面.locator('[data-view="analysis"]').click()
        企业一 = 页面.locator("#analysisCompany option", has_text="600000").first.get_attribute("value")
        企业二 = 页面.locator("#analysisCompany option", has_text="600004").first.get_attribute("value")
        页面.locator("#analysisCompany").select_option(企业一)
        页面.locator("#analysisIndicator").select_option("E_Q_009")
        页面.wait_for_function("() => document.querySelector('#trendMeta').textContent.includes('个年度')", timeout=15_000)
        页面.locator("#analysisYear").select_option("2025")
        页面.locator("#compareCompanies").select_option([企业一, 企业二])
        页面.locator("#compareBtn").click()
        页面.wait_for_selector("#compareResult .comparison-bars article", timeout=15_000)
        结果["desktop"]["trend_meta"] = 页面.locator("#trendMeta").inner_text()
        结果["desktop"]["trend_canvas"] = 画布状态(页面, "trendChart")
        结果["desktop"]["comparison_items"] = 页面.locator("#compareResult .comparison-bars article").count()
        结果["desktop"]["comparison_boundary"] = 页面.locator("#compareResult .boundary-note").inner_text()
        页面.evaluate("window.scrollTo(0, 0)")
        分析截图 = 证据目录 / "桌面端_新版趋势与企业对比.png"
        页面.screenshot(path=str(分析截图), full_page=True)
        结果["screenshots"]["desktop_analysis"] = str(分析截图.relative_to(项目根目录))

        页面.locator('[data-view="upload"]').click()
        页面.wait_for_timeout(300)
        结果["desktop"]["upload_controls"] = 页面.locator("#uploadForm input, #uploadForm select, #uploadForm button").count()
        结果["desktop"]["upload_boundary"] = 页面.locator("#uploadForm .boundary-note").inner_text()
        上传截图 = 证据目录 / "桌面端_新版报告解析.png"
        页面.screenshot(path=str(上传截图), full_page=True)
        结果["screenshots"]["desktop_upload"] = str(上传截图.relative_to(项目根目录))

        移动页 = 浏览器.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        移动错误: list[str] = []
        移动页.on("console", lambda 消息: 移动错误.append(消息.text) if 消息.type == "error" else None)
        移动页.on("pageerror", lambda 异常: 移动错误.append(str(异常)))
        移动页.goto(服务地址, wait_until="networkidle", timeout=60_000)
        移动页.wait_for_selector("#siteHeroTitle", timeout=30_000)
        移动页.wait_for_selector("#summaryKpis .kpi-item", timeout=30_000)
        移动页.evaluate("window.scrollTo(0, 0)")
        移动截图 = 证据目录 / "移动端_新版分析总览.png"
        移动页.screenshot(path=str(移动截图), full_page=True)
        结果["screenshots"]["mobile_overview"] = str(移动截图.relative_to(项目根目录))
        移动页.locator("#siteHeroMenuOpen").click()
        移动菜单截图 = 证据目录 / "移动端_新版全屏菜单.png"
        移动页.screenshot(path=str(移动菜单截图), full_page=False)
        结果["screenshots"]["mobile_menu"] = str(移动菜单截图.relative_to(项目根目录))
        结果["mobile_menu"] = {
            "opened": 移动页.locator("#siteHeroMenu").is_visible(),
            "expanded": 移动页.locator("#siteHeroMenuOpen").get_attribute("aria-expanded"),
            "link_count": 移动页.locator("#siteHeroMenu [data-landing-view]").count(),
        }
        移动页.locator("#siteHeroMenuClose").click()
        结果["mobile_menu"]["closed"] = not 移动页.locator("#siteHeroMenu").is_visible()
        结果["mobile"] = {
            "hero_visible": 移动页.locator(".overview-hero").is_visible(),
            "kpi_count": 移动页.locator("#summaryKpis .kpi-item").count(),
            "document_overflow": 移动页.evaluate("() => ({viewport: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"),
            "overflow_elements": 移动页.evaluate(
                """() => [...document.querySelectorAll('body *')].map(element => {
                  const rect = element.getBoundingClientRect();
                  return {tag: element.tagName, id: element.id, cls: String(element.className), left: rect.left, right: rect.right};
                }).filter(item => item.right > document.documentElement.clientWidth + 2 || item.left < -2).slice(0, 30)"""
            ),
        }
        结果["console_errors"].extend(移动错误)
        移动页.close()
        页面.close()
        浏览器.close()

    输出路径 = 证据目录 / "视觉升级验收结果.json"
    输出路径.write_text(json.dumps(结果, ensure_ascii=False, indent=2), encoding="utf-8")

    assert 结果["desktop"]["title"].startswith("智析绿鉴")
    assert "让 ESG 披露" in 结果["landing"]["heading"]
    assert 结果["landing"]["hero_height"] >= max(600, 结果["landing"]["viewport_height"])
    assert 结果["landing"]["desktop_nav_links"] == 5
    assert 结果["landing"]["report_sheets"] == 2
    assert all(结果["landing"]["video"][属性] for 属性 in ("autoplay", "muted", "loop", "playsInline"))
    assert 结果["landing"]["video"]["poster"].endswith("品牌资产/背景页.jpg")
    assert 结果["landing"]["video"]["source"].endswith(".mp4")
    assert abs(结果["landing"]["cta_workspace_top"]) <= 2
    assert 结果["desktop"]["hero_visible"] is True
    assert 结果["desktop"]["kpi_count"] == 4
    assert 结果["desktop"]["panel_count"] == 4
    assert "服务就绪" in 结果["desktop"]["service_status"]
    assert all(项["nonblank"] for 项 in 结果["desktop"]["canvases"].values())
    assert all(项["visible"] and 项["active_nav"] == "page" for 项 in 结果["views"].values())
    assert "3 个年度" in 结果["desktop"]["trend_meta"]
    assert 结果["desktop"]["trend_canvas"]["nonblank"] is True
    assert 结果["desktop"]["comparison_items"] == 2
    assert "非行业排名" in 结果["desktop"]["comparison_boundary"]
    assert 结果["desktop"]["upload_controls"] >= 7
    assert "运行期上传数据不持久化" in 结果["desktop"]["upload_boundary"]
    assert 结果["desktop"]["document_overflow"]["scroll"] <= 结果["desktop"]["document_overflow"]["viewport"] + 2
    assert 结果["mobile"]["hero_visible"] is True
    assert 结果["mobile"]["kpi_count"] == 4
    assert 结果["mobile"]["document_overflow"]["scroll"] <= 结果["mobile"]["document_overflow"]["viewport"] + 2
    assert 结果["mobile_menu"] == {"opened": True, "expanded": "true", "link_count": 6, "closed": True}
    assert not 结果["console_errors"], 结果["console_errors"]

    print(json.dumps(结果, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
