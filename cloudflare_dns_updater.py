#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import sys
import re
import ipaddress
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkdns.v2 import DnsClient
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
from huaweicloudsdkdns.v2.model import (
    ListPublicZonesRequest,
    ListRecordSetsWithLineRequest,
    UpdateRecordSetRequest,
    UpdateRecordSetReq,
    CreateRecordSetRequest
)

MAX_IP_PER_LINE = 50
CLOUDFLARE_URL = "https://api.uouin.com/cloudflare.html"
CHINA_TZ = timezone(timedelta(hours=8))


def get_bool_env(name, default=False):
    """
    读取布尔环境变量。
    """
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_int_env(name, default):
    """
    读取整数环境变量。
    """
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        print(f"⚠️ 环境变量 {name}={value} 不是整数，使用默认值 {default}")
        return default


def send_telegram(message):
    """
    发送 Telegram 通知
    """
    bot_token = os.environ.get("TG_BOT_TOKEN")
    user_id = os.environ.get("TG_USER_ID")
    
    if not bot_token or not user_id:
        print("⚠️ TG_BOT_TOKEN 或 TG_USER_ID 未设置，跳过通知")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": user_id,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram 通知发送成功")
            return True
        else:
            print(f"❌ Telegram 通知发送失败: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram 通知异常: {str(e)}")
        return False


class HuaWeiApi:
    def __init__(self, ak, sk, region="ap-southeast-1"):
        self.client = DnsClient.new_builder()\
            .with_credentials(BasicCredentials(ak, sk))\
            .with_region(DnsRegion.value_of(region)).build()
        self.zone_id = self._get_zones()

    def _get_zones(self):
        req = ListPublicZonesRequest()
        resp = self.client.list_public_zones(req)
        return {z.name.rstrip('.'): z.id for z in resp.zones}

    def list_records(self, domain, record_type="A", line="默认"):
        zone_id = self.zone_id.get(domain.rstrip('.'))
        if zone_id is None:
            raise KeyError(f"Domain {domain} not in Huawei zone list")
        req = ListRecordSetsWithLineRequest()
        req.zone_id = zone_id
        req.name = f"{domain}."
        req.type = record_type
        req.limit = 100
        resp = self.client.list_record_sets_with_line(req)
        line_map = {
            "默认": "default_view",
            "电信": "Dianxin",
            "联通": "Liantong",
            "移动": "Yidong"
        }
        sdk_line = line_map.get(line, "default_view")
        return [r for r in resp.recordsets if getattr(r, "line", None) == sdk_line]

    def set_records(self, domain, ips, record_type="A", line="默认", ttl=300):
        if not ips:
            print(f"{record_type} | {line} 无有效 IP，跳过更新")
            return

        # 过滤 IP 类型
        if record_type == "A":
            ips = [ip for ip in ips if "." in ip]
        elif record_type == "AAAA":
            ips = [ip for ip in ips if ":" in ip]

        if not ips:
            print(f"{record_type} | {line} 无匹配 IP，跳过")
            return

        # 去重
        ips = list(dict.fromkeys(ips))[:MAX_IP_PER_LINE]

        zone_id = self.zone_id.get(domain.rstrip('.'))
        if zone_id is None:
            raise Exception(f"Domain {domain} not found in zone")

        existing = self.list_records(domain, record_type, line)

        if existing:
            for r in existing:
                existing_vals = list(dict.fromkeys(getattr(r, "records", []) or []))
                if sorted(existing_vals) != sorted(ips):
                    req = UpdateRecordSetRequest()
                    req.zone_id = zone_id
                    req.recordset_id = r.id
                    req.body = UpdateRecordSetReq(
                        name=r.name,
                        type=record_type,
                        ttl=ttl,
                        records=ips
                    )
                    self.client.update_record_set(req)
                    print(f"更新 {line} {record_type} => {ips}")
                else:
                    print(f"{line} {record_type} 无变化，跳过")
        else:
            req = CreateRecordSetRequest()
            req.zone_id = zone_id
            req.body = {
                "name": f"{domain}.",
                "type": record_type,
                "ttl": ttl,
                "records": ips,
                "line": ("default_view" if line == "默认" else
                         ("Dianxin" if line == "电信" else
                          ("Liantong" if line == "联通" else
                           ("Yidong" if line == "移动" else "default_view"))))
            }
            self.client.create_record_set(req)
            print(f"创建 {line} {record_type} => {ips}")



def is_valid_ip(ip):
    """
    校验 IP 地址格式。
    """
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def is_zero_packet_loss(packet_loss):
    """
    判断丢包率是否为 0。
    """
    value = packet_loss.strip().replace("％", "%")
    if not value.endswith("%"):
        return False
    try:
        return float(value[:-1]) == 0
    except ValueError:
        return False


def parse_data_time(value):
    """
    解析页面表格中的时间字段。
    """
    text = value.strip()
    now = datetime.now(CHINA_TZ)

    if not text:
        return None

    if "刚刚" in text:
        return now

    minute_match = re.search(r"(\d+)\s*分钟", text)
    if minute_match:
        return now - timedelta(minutes=int(minute_match.group(1)))

    hour_match = re.search(r"(\d+)\s*小时", text)
    if hour_match:
        return now - timedelta(hours=int(hour_match.group(1)))

    if text.startswith("今天"):
        time_text = text.replace("今天", "").strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(time_text, fmt)
                return now.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)
            except ValueError:
                continue

    formats_with_year = (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y.%m.%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M",
    )
    for fmt in formats_with_year:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CHINA_TZ)
        except ValueError:
            continue

    formats_without_year = (
        "%m/%d %H:%M:%S",
        "%m-%d %H:%M:%S",
        "%m.%d %H:%M:%S",
        "%m/%d %H:%M",
        "%m-%d %H:%M",
        "%m.%d %H:%M",
    )
    for fmt in formats_without_year:
        try:
            parsed = datetime.strptime(text, fmt)
            return now.replace(
                month=parsed.month,
                day=parsed.day,
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second,
                microsecond=0
            )
        except ValueError:
            continue

    return None


def parse_cloudflare_table(html):
    """
    解析渲染后的 Cloudflare 优选 IP 表格。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "table-striped"})
    best = {"默认": [], "电信": [], "联通": [], "移动": [], "IPv6": []}
    full = {}
    data_times = []

    if not table:
        raise Exception("无法获取 Cloudflare IP 表格数据")

    for tr in table.find_all("tr")[1:]:
        cols = [c.text.strip() for c in tr.find_all(["td", "th"])]
        if len(cols) < 9:
            continue

        line = cols[1]
        ip = cols[2]
        packet = cols[3]
        data_time_text = cols[8]

        if not is_valid_ip(ip):
            continue

        if not is_zero_packet_loss(packet):
            continue

        parsed_time = parse_data_time(data_time_text)
        if parsed_time:
            data_times.append(parsed_time)

        if line not in full:
            full[line] = []
        full[line].append({"IP": ip, "带宽": cols[6], "时间": data_time_text})

        # 分类 IP
        if ":" in ip:
            best["IPv6"].append(ip)
        else:
            # 多线 / 全网 / 默认 都算默认
            if line not in ("电信", "联通", "移动"):
                best["默认"].append(ip)
            else:
                best[line].append(ip)

    # 去重 + 限制数量
    for k in best:
        best[k] = list(dict.fromkeys(best[k]))[:MAX_IP_PER_LINE]

    return full, best, data_times


def validate_cloudflare_data(best_ips, data_times):
    """
    校验抓取结果，避免使用空数据或过期数据更新 DNS。
    """
    min_ipv4_count = get_int_env("MIN_IPV4_IP_COUNT", 1)
    max_data_age_hours = get_int_env("MAX_DATA_AGE_HOURS", 24)

    ipv4_count = sum(len(best_ips.get(line, [])) for line in ["默认", "电信", "联通", "移动"])
    if ipv4_count < min_ipv4_count:
        raise Exception(f"IPv4 优选 IP 数量不足，当前 {ipv4_count} 个，最少需要 {min_ipv4_count} 个")

    if not data_times:
        raise Exception("无法解析数据更新时间，拒绝更新 DNS")

    latest_time = max(data_times)
    now = datetime.now(CHINA_TZ)
    oldest_allowed = now - timedelta(hours=max_data_age_hours)
    newest_allowed = now + timedelta(hours=1)

    if latest_time < oldest_allowed:
        raise Exception(
            f"数据已过期，最新数据时间 {latest_time.strftime('%Y/%m/%d %H:%M:%S')}，"
            f"允许最大延迟 {max_data_age_hours} 小时"
        )

    if latest_time > newest_allowed:
        raise Exception(f"数据时间异常，最新数据时间 {latest_time.strftime('%Y/%m/%d %H:%M:%S')}")


def fetch_rendered_html(url):
    """
    使用 Playwright 渲染页面并返回最终 HTML。
    """
    timeout_ms = get_int_env("PLAYWRIGHT_TIMEOUT_MS", 60000)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector("table.table-striped", state="visible", timeout=timeout_ms)
            page.wait_for_function(
                """
                () => {
                    const rows = Array.from(document.querySelectorAll('table.table-striped tr'));
                    return rows.some(row => {
                        const cells = Array.from(row.querySelectorAll('td, th')).map(cell => cell.innerText.trim());
                        const ip = cells[2] || '';
                        return /^(\\d{1,3}\\.){3}\\d{1,3}$/.test(ip) || (ip.includes(':') && /^[0-9a-fA-F:]+$/.test(ip));
                    });
                }
                """,
                timeout=timeout_ms,
            )
            return page.content()
        except PlaywrightTimeoutError as e:
            raise Exception(f"Playwright 页面渲染超时: {e}") from e
        finally:
            browser.close()


def fetch_cloudflare_ips():
    """
    使用 Playwright 渲染页面获取最新 Cloudflare IP。
    """
    html = fetch_rendered_html(CLOUDFLARE_URL)
    full, best, data_times = parse_cloudflare_table(html)
    validate_cloudflare_data(best, data_times)
    return full, best


if __name__ == "__main__":
    full_domain = os.environ.get("FULL_DOMAIN")
    ak = os.environ.get("HUAWEI_ACCESS_KEY")
    sk = os.environ.get("HUAWEI_SECRET_KEY")
    region = os.environ.get("HUAWEI_REGION", "ap-southeast-1")
    enable_ipv6_dns_sync = get_bool_env("ENABLE_IPV6_DNS_SYNC", False)

    if not all([full_domain, ak, sk]):
        error_msg = "环境变量 FULL_DOMAIN / HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY 必须设置"
        print(error_msg)
        send_telegram(f"🚨 <b>DNS 更新失败</b>\n\n❌ {error_msg}")
        sys.exit(1)

    try:
        print(f"开始更新 DNS: {full_domain}")
        print(f"IPv6 DNS 同步: {'启用' if enable_ipv6_dns_sync else '关闭'}")

        # 初始化华为云 API
        hw = HuaWeiApi(ak, sk, region)
        
        # 获取 Cloudflare IP
        full_data, best_ips = fetch_cloudflare_ips()
        
        # 统计更新信息
        update_summary = []

        # 更新 IPv4
        for line in ["默认", "电信", "联通", "移动"]:
            ip_list = best_ips.get(line, [])
            if ip_list:
                hw.set_records(full_domain, ip_list, record_type="A", line=line)
                update_summary.append(f"✅ {line} A记录: {len(ip_list)} 个IP")

        # 更新 IPv6：默认关闭，仅控制 DNS 同步，不影响数据抓取和输出文件
        ip_list_v6 = best_ips.get("IPv6", [])
        if enable_ipv6_dns_sync and ip_list_v6:
            hw.set_records(full_domain, ip_list_v6, record_type="AAAA", line="默认")
            update_summary.append(f"✅ IPv6 AAAA记录: {len(ip_list_v6)} 个IP")
        elif ip_list_v6:
            print("IPv6 DNS 同步已关闭，跳过 AAAA 记录更新")
            update_summary.append(f"⏭️ IPv6 AAAA记录: 已关闭同步，抓取到 {len(ip_list_v6)} 个IP")

        # 保存 JSON
        with open("cloudflare_bestip.json", "w", encoding="utf-8") as f:
            json.dump({"最优IP": best_ips, "完整数据": full_data}, f, ensure_ascii=False, indent=4)
        print("JSON 文件保存到 cloudflare_bestip.json")

        # 保存 TXT 文件（使用北京时间）
        now = datetime.now(CHINA_TZ).strftime("%Y/%m/%d %H:%M:%S")
        txt_lines = []

        for line in ["默认", "电信", "联通", "移动", "IPv6"]:
            ip_list = best_ips.get(line, [])
            if not ip_list:
                continue
            txt_lines.append(now)
            for ip in ip_list:
                if ":" in ip:  # IPv6
                    txt_lines.append(f"[{ip}]#{line}")
                else:
                    txt_lines.append(f"{ip}#{line}")
            txt_lines.append("")  # 每组之间空行

        with open("cloudflare_bestip.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        print("TXT 文件保存到 cloudflare_bestip.txt")
        
        # 发送成功通知（可选）
        success_msg = f"""✅ <b>DNS 更新成功</b>

📋 域名: <code>{full_domain}</code>
🕐 时间: {now}

{chr(10).join(update_summary)}
"""
        send_telegram(success_msg)
        print("✅ DNS 更新完成")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 错误: {error_msg}")
        
        # 发送失败通知
        now = datetime.now(CHINA_TZ).strftime("%Y/%m/%d %H:%M:%S")
        
        fail_msg = f"""🚨 <b>DNS 更新失败</b>

📋 域名: <code>{full_domain}</code>
🕐 时间: {now}
❌ 错误: <code>{error_msg}</code>

请检查日志并手动处理！
"""
        send_telegram(fail_msg)
        sys.exit(1)
