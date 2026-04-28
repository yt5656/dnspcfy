#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import sys
import time
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

        if record_type == "A":
            ips = filter_valid_ips(ips, 4)
            min_count = MIN_IPV4_COUNT
        else:
            min_count = 1

        if not ips:
            print(f"{record_type} | {line} 无匹配 IP，跳过")
            return False

        if len(ips) < min_count:
            print(f"⚠️ {record_type} | {line} 有效 IP 数量 {len(ips)} < {min_count}，跳过更新，避免污染 DNS")
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
                    return True
                else:
                    print(f"{line} {record_type} 无变化，跳过")
                    return False
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
            return True


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
            continue

        ip_obj = ipaddress.ip_address(ip)
        
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
    """安全无泄漏的动态抓取，加入强制进程释放"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    print("🚀 强制使用无头浏览器 (requests-html) 动态渲染抓取...")
    from requests_html import HTMLSession
    session = HTMLSession()
    nocache_url = f"{SOURCE_URL}?t={int(time.time())}"
    
    try:
        r = session.get(nocache_url, headers=headers, timeout=20)
        r.html.render(sleep=6, timeout=20)
        return parse_cloudflare_html(r.html.html)
    except Exception as e:
        raise Exception(f"动态浏览器渲染抓取彻底失败: {e}")
    finally:
        # 【深度修复 2】无论成功失败，强制关闭 Chromium 进程，防止僵尸进程 OOM
        try:
            session.close()
        except:
            pass


def fetch_cloudflare_ips_with_retry(max_retries=3):
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
        print("环境变量缺失")
        sys.exit(1)

    try:
        print(f"开始更新 DNS: {full_domain}")
        hw = HuaWeiApi(ak, sk, region)
        full_data, best_ips = fetch_cloudflare_ips_with_retry()
        best_ips = protect_best_ips(best_ips)
        
        update_summary = []
        has_real_update = False

        for line in ["默认", "电信", "联通", "移动"]:
            ip_list = best_ips.get(line, [])
            if ip_list:
                updated = hw.set_records(full_domain, ip_list, record_type="A", line=line)
                if updated:
                    update_summary.append(f"✅ {line} A记录: {len(ip_list)} 个IP")
                    has_real_update = True

        # 【深度修复 1】解决 Git 脏提交漏洞
        # 仅在实际发生更新，或本地文件不存在（首次运行）时，才写入数据文件
        if has_real_update or not os.path.exists("cloudflare_bestip.json"):
            with open("cloudflare_bestip.json", "w", encoding="utf-8") as f:
                json.dump({"最优IP": best_ips, "完整数据": full_data}, f, ensure_ascii=False, indent=4)
            print("JSON 文件已更新")

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
            print("TXT 文件已更新")
        else:
            print("🛑 优选 IP 无实质变化，忽略本地文件覆盖以保持 Git 整洁。")

        china_tz = timezone(timedelta(hours=8))
        now = datetime.now(china_tz).strftime("%Y/%m/%d %H:%M:%S")

        if has_real_update:
            success_msg = f"✅ <b>DNS 记录已自动变更</b>\n\n📋 域名: <code>{full_domain}</code>\n🕐 时间: {now}\n\n{chr(10).join(update_summary)}"
            send_telegram(success_msg)
            print("✅ DNS 变更完成，已推送 TG 通知")
        else:
            print("💤 优选 IP 无变动，流程结束。")

    except Exception as e:
        print(f"❌ 错误: {str(e)}")
        sys.exit(1)
