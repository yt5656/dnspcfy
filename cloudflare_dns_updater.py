#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import sys
import time  # 新增：用于网络重试的等待时间
import ipaddress
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
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

MAX_IP_PER_LINE = int(os.environ.get("MAX_IP_PER_LINE", "50"))
MIN_IPV4_COUNT = int(os.environ.get("MIN_IPV4_COUNT", "3"))
SOURCE_URL = "https://api.uouin.com/cloudflare.html"


def is_valid_ip(ip, version=None):
    try:
        ip_obj = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return version is None or ip_obj.version == version


def filter_valid_ips(ips, version):
    result = []
    for ip in ips or []:
        ip = str(ip).strip()
        if is_valid_ip(ip, version):
            result.append(ip)
        else:
            print(f"⚠️ 过滤非法 IP: {ip}")
    return list(dict.fromkeys(result))[:MAX_IP_PER_LINE]


def send_telegram(message):
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
            return False

        # 仅处理 A 记录
        if record_type == "A":
            ips = filter_valid_ips(ips, 4)
            min_count = MIN_IPV4_COUNT
        else:
            min_count = 1

        if not ips:
            print(f"{record_type} | {line} 无匹配 IP，跳过")
            return False

        if len(ips) < min_count:
            print(f"⚠️ {record_type} | {line} 有效 IP 数量 {len(ips)} < {min_count}，跳过更新，避免异常数据污染 DNS")
            return False

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
                    return True  # 记录发生了真实变更
                else:
                    print(f"{line} {record_type} 无变化，跳过")
                    return False # 记录无变更
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
            return True  # 创建了新记录


def parse_cloudflare_html(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "table-striped"}) or soup.find("table")
    best = {"默认": [], "电信": [], "联通": [], "移动": []}
    full = {}

    if not table:
        raise Exception("无法获取 Cloudflare IP 表格数据")

    for tr in table.find_all("tr")[1:]:
        cols = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cols) < 9:
            continue

        line = cols[1]
        ip = cols[2].strip()
        packet = cols[3].strip()

        if packet != "0.00%":
            continue

        if not is_valid_ip(ip):
            print(f"⚠️ 页面数据不是合法 IP，跳过: {ip}")
            continue

        ip_obj = ipaddress.ip_address(ip)
        
        # 仅处理 IPv4
        if ip_obj.version == 4:
            if line not in full:
                full[line] = []
            full[line].append({"IP": ip, "带宽": cols[6], "时间": cols[8]})
            
            if line not in ("电信", "联通", "移动"):
                best["默认"].append(ip)
            else:
                best[line].append(ip)

    for k in best:
        best[k] = filter_valid_ips(best[k], 4)

    if not any(best.values()):
        raise Exception("未解析到任何 0 丢包的合法 Cloudflare IP")

    return full, best


def fetch_cloudflare_ips():
    # 核心技巧 1：在网址后面加一个随机时间戳参数 (例如 ?t=1714270000)
    # 这样每次请求的 URL 都是全新的，强迫 CDN 回源站拉取最新数据，避开缓存
    nocache_url = f"{SOURCE_URL}?t={int(time.time())}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        # 核心技巧 2：在请求头里明确告诉服务器：不要给我发缓存！
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    }

    try:
        print("使用普通 requests 抓取 Cloudflare 优选页面...")
        # 注意这里用的是加了时间戳的 nocache_url
        r = requests.get(nocache_url, headers=headers, timeout=20)
        r.raise_for_status()
        return parse_cloudflare_html(r.text)
    except Exception as e:
        print(f"⚠️ 普通抓取失败，切换 requests-html 渲染 fallback: {e}")

    try:
        from requests_html import HTMLSession
        session = HTMLSession()
        # Fallback 抓取也用强力破缓存的 URL
        r = session.get(nocache_url, headers=headers, timeout=20)
        r.html.render(sleep=6, timeout=20)
        return parse_cloudflare_html(r.html.html)
    except Exception as e:
        raise Exception(f"Cloudflare IP 页面抓取失败: {e}")



def fetch_cloudflare_ips_with_retry(max_retries=3):
    """带重试机制的抓取函数，解决偶尔的网络波动"""
    for attempt in range(max_retries):
        try:
            return fetch_cloudflare_ips()
        except Exception as e:
            print(f"⚠️ 抓取出错 (第 {attempt + 1}/{max_retries} 次): {e}")
            if attempt < max_retries - 1:
                print("等待 5 秒后自动重试...")
                time.sleep(5)
            else:
                raise Exception(f"连续 {max_retries} 次抓取失败，放弃执行。")


def protect_best_ips(best_ips):
    protected = {"默认": [], "电信": [], "联通": [], "移动": []}

    for line in ["默认", "电信", "联通", "移动"]:
        ips = filter_valid_ips(best_ips.get(line, []), 4)
        if len(ips) < MIN_IPV4_COUNT:
            print(f"⚠️ {line} A记录有效 IP 数量 {len(ips)} < {MIN_IPV4_COUNT}，本线路不更新")
            continue
        protected[line] = ips

    if not any(protected.values()):
        raise Exception("所有线路有效 IP 数量均不足，取消 DNS 更新")

    return protected


if __name__ == "__main__":
    full_domain = os.environ.get("FULL_DOMAIN")
    ak = os.environ.get("HUAWEI_ACCESS_KEY")
    sk = os.environ.get("HUAWEI_SECRET_KEY")
    region = os.environ.get("HUAWEI_REGION", "ap-southeast-1")

    if not all([full_domain, ak, sk]):
        error_msg = "环境变量 FULL_DOMAIN / HUAWEI_ACCESS_KEY / HUAWEI_SECRET_KEY 必须设置"
        print(error_msg)
        send_telegram(f"🚨 <b>DNS 更新失败</b>\n\n❌ {error_msg}")
        sys.exit(1)

    try:
        print(f"开始更新 DNS: {full_domain}")
        
        hw = HuaWeiApi(ak, sk, region)
        
        # 使用带有 3 次重试机制的抓取函数
        full_data, best_ips = fetch_cloudflare_ips_with_retry()
        best_ips = protect_best_ips(best_ips)
        
        update_summary = []
        has_real_update = False  # 变更标记

        # 仅更新 IPv4
        for line in ["默认", "电信", "联通", "移动"]:
            ip_list = best_ips.get(line, [])
            if ip_list:
                # 获取是否发生了实际更新的布尔值
                updated = hw.set_records(full_domain, ip_list, record_type="A", line=line)
                if updated:
                    update_summary.append(f"✅ {line} A记录: {len(ip_list)} 个IP")
                    has_real_update = True

        # 保存 JSON 数据文件
        with open("cloudflare_bestip.json", "w", encoding="utf-8") as f:
            json.dump({"最优IP": best_ips, "完整数据": full_data}, f, ensure_ascii=False, indent=4)
        print("JSON 文件保存到 cloudflare_bestip.json")

        # 生成纯净的 TXT 文件（去掉了时间戳，避免无意义的 Git Commit）
        txt_lines = []
        for line in ["默认", "电信", "联通", "移动"]:
            ip_list = best_ips.get(line, [])
            if not ip_list:
                continue
            for ip in ip_list:
                txt_lines.append(f"{ip}#{line}")
            txt_lines.append("")

        with open("cloudflare_bestip.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        print("TXT 文件保存到 cloudflare_bestip.txt")
        
        # 智能通知：仅在发生真实 IP 变动时才发送 Telegram 消息
        china_tz = timezone(timedelta(hours=8))
        now = datetime.now(china_tz).strftime("%Y/%m/%d %H:%M:%S")

        if has_real_update:
            success_msg = f"""✅ <b>DNS 记录已自动变更</b>

📋 域名: <code>{full_domain}</code>
🕐 时间: {now}

{chr(10).join(update_summary)}
"""
            send_telegram(success_msg)
            print("✅ DNS 变更完成，已推送 TG 通知")
        else:
            print("💤 优选 IP 无实质变化，未触发真实更新，跳过 Telegram 通知。")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 错误: {error_msg}")
        
        china_tz = timezone(timedelta(hours=8))
        now = datetime.now(china_tz).strftime("%Y/%m/%d %H:%M:%S")
        
        fail_msg = f"""🚨 <b>DNS 更新运行异常</b>

📋 域名: <code>{full_domain}</code>
🕐 时间: {now}
❌ 错误: <code>{error_msg}</code>

请检查 GitHub Actions 日志！
"""
        send_telegram(fail_msg)
        sys.exit(1)
