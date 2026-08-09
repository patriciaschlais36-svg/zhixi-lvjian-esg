#!/usr/bin/env python3
"""按显式来源清单合规采集ESG报告，并登记到真实分析队列。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


平台服务目录 = Path(__file__).resolve().parents[1]
if str(平台服务目录) not in sys.path:
    sys.path.insert(0, str(平台服务目录))

from 数据服务 import 数据服务, 当前时间  # noqa: E402


必需字段 = ("stock_code", "company_name", "report_year", "report_title", "source_url")
允许报告类型 = {"ESG", "SD", "CSR", "ENV", "OTHER"}


@dataclass(frozen=True)
class 采集项:
    股票代码: str
    企业简称: str
    报告年份: int
    报告标题: str
    报告类型: str
    来源网址: str
    来源公告编号: str


def _启用值(文本: str) -> bool:
    return str(文本 or "").strip().lower() not in {"0", "false", "no", "否", "禁用"}


def 读取采集清单(路径: Path) -> list[采集项]:
    with 路径.open("r", encoding="utf-8-sig", newline="") as 文件:
        读取器 = csv.DictReader(文件)
        缺少 = [字段 for 字段 in 必需字段 if 字段 not in (读取器.fieldnames or [])]
        if 缺少:
            raise ValueError(f"采集清单缺少字段：{','.join(缺少)}")
        结果: list[采集项] = []
        for 行号, 行 in enumerate(读取器, start=2):
            if not _启用值(行.get("enabled", "1")):
                continue
            股票代码 = str(行.get("stock_code", "")).strip()
            企业简称 = str(行.get("company_name", "")).strip()
            报告标题 = str(行.get("report_title", "")).strip()
            来源网址 = str(行.get("source_url", "")).strip()
            报告类型 = str(行.get("report_type", "ESG") or "ESG").strip().upper()
            try:
                报告年份 = int(str(行.get("report_year", "")).strip())
            except ValueError as 异常:
                raise ValueError(f"采集清单第{行号}行报告年份不是整数") from 异常
            if len(股票代码) != 6 or not 股票代码.isdigit():
                raise ValueError(f"采集清单第{行号}行股票代码必须为6位数字")
            if not 企业简称 or not 报告标题:
                raise ValueError(f"采集清单第{行号}行企业简称和报告标题不能为空")
            if not 2000 <= 报告年份 <= 2100:
                raise ValueError(f"采集清单第{行号}行报告年份超出允许范围")
            if 报告类型 not in 允许报告类型:
                raise ValueError(f"采集清单第{行号}行报告类型不受支持")
            解析 = urllib.parse.urlparse(来源网址)
            if 解析.scheme.lower() != "https" or not 解析.hostname:
                raise ValueError(f"采集清单第{行号}行来源网址必须为有效HTTPS地址")
            结果.append(
                采集项(
                    股票代码=股票代码,
                    企业简称=企业简称,
                    报告年份=报告年份,
                    报告标题=报告标题,
                    报告类型=报告类型,
                    来源网址=urllib.parse.urlunparse(解析._replace(fragment="")),
                    来源公告编号=str(行.get("source_notice_id", "")).strip(),
                )
            )
    if not 结果:
        raise ValueError("采集清单没有启用的报告记录")
    return 结果


def _规范域名(域名: str) -> str:
    return str(域名 or "").strip().lower().rstrip(".")


def _域名允许(主机: str, 允许域名: set[str]) -> bool:
    主机 = _规范域名(主机)
    return any(主机 == 域名 or 主机.endswith(f".{域名}") for 域名 in 允许域名)


def 校验网址边界(网址: str, 允许域名: set[str], *, 解析DNS: bool) -> str:
    解析 = urllib.parse.urlparse(网址)
    主机 = _规范域名(解析.hostname or "")
    if 解析.scheme.lower() != "https" or not 主机:
        raise ValueError("只允许采集HTTPS地址")
    if not _域名允许(主机, 允许域名):
        raise ValueError(f"来源域名不在显式允许列表：{主机}")
    try:
        地址 = ipaddress.ip_address(主机)
    except ValueError:
        地址 = None
    if 地址 and (地址.is_private or 地址.is_loopback or 地址.is_link_local or 地址.is_reserved):
        raise ValueError("禁止采集本机、内网、链路本地或保留地址")
    if 解析DNS:
        try:
            记录 = socket.getaddrinfo(主机, 解析.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as 异常:
            raise ValueError(f"来源域名无法解析：{主机}") from 异常
        for 记录项 in 记录:
            地址 = ipaddress.ip_address(记录项[4][0])
            if 地址.is_private or 地址.is_loopback or 地址.is_link_local or 地址.is_reserved:
                raise ValueError(f"来源域名解析到非公网地址：{主机}")
    return 主机


class 合规采集器:
    def __init__(
        self, *, 允许域名: Iterable[str], 用户代理: str, 超时秒数: int,
        最大字节: int, robots失败时拒绝: bool, 重试次数: int,
    ) -> None:
        self.允许域名 = {_规范域名(x) for x in 允许域名 if _规范域名(x)}
        if not self.允许域名:
            raise ValueError("必须至少提供一个显式允许域名")
        self.用户代理 = 用户代理.strip()
        if len(self.用户代理) < 8:
            raise ValueError("用户代理过短；应提供可识别的研究采集器名称")
        self.超时秒数 = 超时秒数
        self.最大字节 = 最大字节
        self.robots失败时拒绝 = robots失败时拒绝
        self.重试次数 = 重试次数
        self._robots缓存: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _请求(self, 网址: str, 接受类型: str) -> urllib.response.addinfourl:
        最后异常: Exception | None = None
        for 次数 in range(self.重试次数 + 1):
            try:
                请求 = urllib.request.Request(
                    网址,
                    headers={"User-Agent": self.用户代理, "Accept": 接受类型},
                )
                return urllib.request.urlopen(请求, timeout=self.超时秒数)
            except urllib.error.HTTPError as 异常:
                最后异常 = 异常
                if 异常.code not in {429, 500, 502, 503, 504} or 次数 >= self.重试次数:
                    raise
                等待 = min(30, max(1, int(异常.headers.get("Retry-After", "0") or 2 ** 次数)))
                time.sleep(等待)
            except urllib.error.URLError as 异常:
                最后异常 = 异常
                if 次数 >= self.重试次数:
                    raise
                time.sleep(min(8, 2 ** 次数))
        raise RuntimeError("网络请求失败") from 最后异常

    def _robots允许(self, 网址: str) -> bool:
        解析 = urllib.parse.urlparse(网址)
        来源 = f"{解析.scheme}://{解析.netloc}"
        if 来源 not in self._robots缓存:
            robots网址 = f"{来源}/robots.txt"
            解析器 = urllib.robotparser.RobotFileParser()
            try:
                with self._请求(robots网址, "text/plain,*/*;q=0.1") as 响应:
                    最终网址 = 响应.geturl()
                    校验网址边界(最终网址, self.允许域名, 解析DNS=True)
                    文本 = 响应.read(1024 * 1024).decode("utf-8", errors="replace")
                解析器.set_url(robots网址)
                解析器.parse(文本.splitlines())
                self._robots缓存[来源] = 解析器
            except urllib.error.HTTPError as 异常:
                if 异常.code == 404:
                    self._robots缓存[来源] = None
                elif self.robots失败时拒绝:
                    raise RuntimeError(f"无法核验robots.txt：HTTP {异常.code}") from 异常
                else:
                    self._robots缓存[来源] = None
            except (OSError, ValueError, urllib.error.URLError) as 异常:
                if self.robots失败时拒绝:
                    raise RuntimeError("无法核验robots.txt，已按失败时拒绝策略停止") from 异常
                self._robots缓存[来源] = None
        解析器 = self._robots缓存[来源]
        return True if 解析器 is None else 解析器.can_fetch(self.用户代理, 网址)

    def 下载PDF(self, 网址: str, 暂存目录: Path) -> tuple[Path, dict[str, object]]:
        校验网址边界(网址, self.允许域名, 解析DNS=True)
        if not self._robots允许(网址):
            raise RuntimeError("robots.txt禁止当前用户代理采集该地址")
        暂存目录.mkdir(parents=True, exist_ok=True)
        文件描述符, 文件名 = tempfile.mkstemp(prefix="采集_", suffix=".pdf", dir=暂存目录)
        os.close(文件描述符)
        暂存路径 = Path(文件名)
        摘要器 = hashlib.sha256()
        大小 = 0
        try:
            with self._请求(网址, "application/pdf") as 响应, 暂存路径.open("wb") as 输出:
                最终网址 = 响应.geturl()
                最终主机 = 校验网址边界(最终网址, self.允许域名, 解析DNS=True)
                长度文本 = 响应.headers.get("Content-Length", "").strip()
                if 长度文本.isdigit() and int(长度文本) > self.最大字节:
                    raise ValueError("远程文件超过允许大小")
                while 数据 := 响应.read(64 * 1024):
                    大小 += len(数据)
                    if 大小 > self.最大字节:
                        raise ValueError("下载内容超过允许大小")
                    摘要器.update(数据)
                    输出.write(数据)
                内容类型 = 响应.headers.get_content_type()
            with 暂存路径.open("rb") as 文件:
                文件头 = 文件.read(5)
                文件.seek(max(0, 大小 - 4096))
                文件尾 = 文件.read()
            if 文件头 != b"%PDF-":
                raise ValueError("远程内容不是PDF文件")
            return 暂存路径, {
                "source_url": 网址,
                "final_url": 最终网址,
                "source_host": 最终主机,
                "content_type": 内容类型,
                "file_size_bytes": 大小,
                "sha256": 摘要器.hexdigest(),
                "pdf_eof_ok": b"%%EOF" in 文件尾,
            }
        except Exception:
            暂存路径.unlink(missing_ok=True)
            raise


def _来源编号(项目: 采集项) -> str:
    if 项目.来源公告编号:
        return 项目.来源公告编号
    return "url-" + hashlib.sha256(项目.来源网址.encode("utf-8")).hexdigest()[:24]


def 执行采集(args: argparse.Namespace) -> int:
    清单 = 读取采集清单(args.清单)
    允许域名 = {_规范域名(x) for x in args.允许域名}
    for 项目 in 清单:
        校验网址边界(项目.来源网址, 允许域名, 解析DNS=False)
    if not args.执行:
        print(json.dumps({
            "mode": "plan_only",
            "enabled_reports": len(清单),
            "allowed_domains": sorted(允许域名),
            "robots_policy": "deny_on_error" if not args.允许robots不可用 else "allow_on_error",
            "message": "未传入--执行；没有发起网络请求、下载文件或修改数据库。",
        }, ensure_ascii=False, indent=2))
        return 0

    服务 = 数据服务()
    服务.初始化()
    采集器 = 合规采集器(
        允许域名=允许域名,
        用户代理=args.用户代理,
        超时秒数=args.超时秒数,
        最大字节=args.最大MB * 1024 * 1024,
        robots失败时拒绝=not args.允许robots不可用,
        重试次数=args.重试次数,
    )
    记录目录 = 服务.路径.运行目录 / "采集记录"
    记录目录.mkdir(parents=True, exist_ok=True)
    记录路径 = 记录目录 / f"采集_{当前时间().replace(':', '').replace('-', '')}.jsonl"
    成功数 = 0
    失败数 = 0
    with 记录路径.open("w", encoding="utf-8") as 记录文件:
        for 序号, 项目 in enumerate(清单, start=1):
            暂存路径: Path | None = None
            try:
                暂存路径, 下载信息 = 采集器.下载PDF(项目.来源网址, 服务.路径.运行目录 / "采集暂存")
                幂等材料 = f"{项目.来源网址}|{项目.股票代码}|{项目.报告年份}|{下载信息['sha256']}"
                登记 = 服务.登记上传并创建任务(
                    暂存路径,
                    股票代码=项目.股票代码,
                    报告年份=项目.报告年份,
                    企业简称=项目.企业简称,
                    报告标题=项目.报告标题,
                    原始文件名=f"{项目.股票代码}_{项目.报告年份}_{项目.报告类型}.pdf",
                    报告类型=项目.报告类型,
                    来源站点=str(下载信息["source_host"]),
                    来源公告编号=_来源编号(项目),
                    请求幂等键="collector-" + hashlib.sha256(幂等材料.encode("utf-8")).hexdigest(),
                    最大字节=args.最大MB * 1024 * 1024,
                )
                载荷 = {
                    "status": "queued",
                    "sequence": 序号,
                    "report": asdict(项目),
                    "download": 下载信息,
                    "registration": 登记,
                    "recorded_at": 当前时间(),
                }
                成功数 += 1
            except Exception as 异常:
                载荷 = {
                    "status": "failed",
                    "sequence": 序号,
                    "report": asdict(项目),
                    "error_type": type(异常).__name__,
                    "error": str(异常)[:1000],
                    "recorded_at": 当前时间(),
                }
                失败数 += 1
            finally:
                if 暂存路径 is not None:
                    暂存路径.unlink(missing_ok=True)
            记录文件.write(json.dumps(载荷, ensure_ascii=False) + "\n")
            记录文件.flush()
            if 序号 < len(清单):
                time.sleep(args.请求间隔秒数)
    print(json.dumps({
        "status": "completed" if 失败数 == 0 else "partial",
        "queued": 成功数,
        "failed": 失败数,
        "record_file": str(记录路径),
        "message": "采集器只负责校验、下载、去重和入队；指标结果由平台真实任务执行器生成。",
    }, ensure_ascii=False, indent=2))
    return 0 if 失败数 == 0 else 2


def 构建解析器() -> argparse.ArgumentParser:
    解析器 = argparse.ArgumentParser(description="按显式来源清单合规采集ESG报告")
    解析器.add_argument("清单", type=Path, help="UTF-8 CSV来源清单")
    解析器.add_argument("--允许域名", action="append", required=True, help="可重复填写；允许该域名及其子域")
    解析器.add_argument("--用户代理", default="ZhixiLvjianESGCollector/1.0", help="建议附研究用途和联系渠道")
    解析器.add_argument("--超时秒数", type=int, default=30)
    解析器.add_argument("--最大MB", type=int, default=30)
    解析器.add_argument("--请求间隔秒数", type=float, default=2.0)
    解析器.add_argument("--重试次数", type=int, default=2)
    解析器.add_argument("--允许robots不可用", action="store_true", help="robots.txt无法获取时继续；默认失败即拒绝")
    解析器.add_argument("--执行", action="store_true", help="未传入时只校验清单和边界，不访问网络")
    return 解析器


def main(argv: Sequence[str] | None = None) -> int:
    args = 构建解析器().parse_args(argv)
    if not args.清单.is_file():
        raise SystemExit(f"未找到采集清单：{args.清单}")
    if not 1 <= args.最大MB <= 200:
        raise SystemExit("--最大MB必须在1至200之间")
    if not 1 <= args.超时秒数 <= 300:
        raise SystemExit("--超时秒数必须在1至300之间")
    if not 0 <= args.请求间隔秒数 <= 3600:
        raise SystemExit("--请求间隔秒数必须在0至3600之间")
    if not 0 <= args.重试次数 <= 5:
        raise SystemExit("--重试次数必须在0至5之间")
    try:
        return 执行采集(args)
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as 异常:
        raise SystemExit(str(异常)) from 异常


if __name__ == "__main__":
    raise SystemExit(main())

