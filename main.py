# -*- coding: UTF-8 -*-

import hashlib
import json
import os
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from requests.exceptions import RequestException

TOKEN_LIST = os.getenv('TOKEN_LIST', '')
SEND_KEY_LIST = os.getenv('SEND_KEY_LIST', '')

# 接口配置
url = 'https://m.jlc.com/api/activity/sign/signIn?source=3'
gold_bean_url = "https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo"
seventh_day_url = "https://m.jlc.com/api/activity/sign/receiveVoucher"

# ======== 当日签到记忆 (防风控) ========
# token 匿名指纹做 key: 记忆文件只存 sha256 前 12 位, 不落明文 token;
# 已签到(ok/already)的账号当天再跑直接跳过, 零请求; 失败不记, 下个 6 小时周期自然重试;
# 记忆保留 30 天滚动清理。GHA 侧由 actions/cache 持久化本文件 (兼作仓库保活)。
# 日期 key 钉死东八区: GHA runner 是 UTC, 北京 00 点的 cron 在 UTC 是前一天 16 点,
# 用系统时区会取到前一天日期 -> 撞上前一天记忆 -> 新一天签到被跳过。

TZ_CN = timezone(timedelta(hours=8))

STATE_FILE = Path(__file__).resolve().parent / '.checkin_state.json'
STATE_KEEP_DAYS = 30


def today_str():
    return datetime.now(TZ_CN).strftime('%Y-%m-%d')


def token_key(token):
    """token 的匿名指纹 (跨 run 稳定, 不暴露明文)"""
    return hashlib.sha256(token.strip().encode('utf-8')).hexdigest()[:12]


def load_state():
    if not STATE_FILE.exists():
        return {"days": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {"days": {}}


def save_state(state):
    # 30 天滚动清理
    keep_after = (datetime.now(TZ_CN) - timedelta(days=STATE_KEEP_DAYS)).strftime("%Y-%m-%d")
    state["days"] = {d: v for d, v in state.get("days", {}).items() if d >= keep_after}
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding='utf-8')
    except Exception as e:
        print(f"⚠️ 状态写入失败: {e}")


def mark_done(state, today, key, status, customer_code=None):
    # 值改为 dict: status + 完整客编, 供跳过行显示; 兼容旧的字符串值
    state["days"].setdefault(today, {})[key] = {
        "status": status,
        "customerCode": customer_code,
    }


# ======== 工具函数 ========

def mask_account(account):
    """用于打印时隐藏部分账号信息 (仅用于 token, 客编已明文输出)"""
    if len(account) >= 4:
        return account[:2] + '****' + account[-2:]
    return '****'


# ======== 邮件通知 (内联, 无需额外文件) ========
# 触发: 任意一个账号 fail -> 发邮件; 全部成功或今日已签到 -> 不发。
# 配置: SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_SECURE /
#       MAIL_FROM / MAIL_TO; 未配 SMTP_HOST 时回落同目录 config.json 的 smtp 块。
# 163/yeah.net 的 SMTP_PASS 必须是客户端授权码, 不是登录密码。
# 未配置任何 SMTP 时静默跳过, 不影响签到主流程。

DEFAULT_MAIL_TO = ["208315339@qq.com", "althaeaa@yeah.net"]


def _smtp_settings():
    """env 优先, 回落 config.json 的 smtp 块 (本地免配环境变量用)"""
    s = {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER", ""),
        "pass": os.environ.get("SMTP_PASS", ""),
        "secure": os.environ.get("SMTP_SECURE", "true").lower() in ("1", "true", "yes"),
        "mail_from": os.environ.get("MAIL_FROM", "althaeaa@yeah.net"),
    }
    if not s["host"]:
        try:
            cfg_file = Path(__file__).resolve().parent / "config.json"
            if cfg_file.exists():
                fc = json.loads(cfg_file.read_text(encoding="utf-8")).get("smtp") or {}
                s = {
                    "host": fc.get("host", ""),
                    "port": int(fc.get("port", 465)),
                    "user": fc.get("user", ""),
                    "pass": fc.get("pass", ""),
                    "secure": bool(fc.get("secure", True)),
                    "mail_from": fc.get("from") or fc.get("mail_from", ""),
                }
        except Exception:
            pass
    return s


def notify_email(title, body, mail_to=""):
    """SMTP 邮件通知 (标准库 smtplib)。未配置 SMTP 则静默跳过。
    收件人支持 list 或逗号分隔字符串, 一次发给全部地址。"""
    s = _smtp_settings()
    raw = mail_to or os.environ.get("MAIL_TO") or DEFAULT_MAIL_TO
    if isinstance(raw, str):
        recipients = [a.strip() for a in raw.split(',') if a.strip()]
    else:
        recipients = [a for a in raw if a]
    if not s["host"] or not recipients:
        return
    if not s["mail_from"]:
        print("[mail] WARN: SMTP 已配主机但无 MAIL_FROM/SMTP_USER, 跳过邮件")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = s["mail_from"]
        msg["To"] = ", ".join(recipients)
        if s["secure"]:
            server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=15)
        else:
            server = smtplib.SMTP(s["host"], s["port"], timeout=15)
            server.starttls()
        server.login(s["user"], s["pass"])
        server.sendmail(s["mail_from"], recipients, msg.as_string())
        server.quit()
        print(f"[mail] 邮件通知已发送 -> {', '.join(recipients)}")
    except Exception as e:
        print(f"[mail] WARN: 邮件通知失败: {type(e).__name__}: {e}")


# ======== 推送通知 ========

def send_msg_by_server(send_key, title, content):
    push_url = f'https://sctapi.ftqq.com/{send_key}.send'
    data = {
        'text': title,
        'desp': content
    }
    try:
        response = requests.post(push_url, data=data, timeout=15)
        response.raise_for_status()
        return response.json()
    except RequestException as e:
        print(f"❌ 推送请求异常: {str(e)}")
        return None
    except Exception as e:
        print(f"❌ 推送未知异常: {str(e)}")
        return None


# ======== 单个账号签到逻辑 ========
# 返回 (status, message, customer_code):
#   status: ok=今日签到成功 / already=今日已签到 / fail=失败(未记记忆, 下周期重试)
#   message: 仅 ok 时非空 (推送到 Server 酱的内容)
#   customer_code: 完整客编, 写入记忆供跳过行显示

def sign_in(access_token, retries=3, backoff=10):
    customer_code = None  # 首个请求失败时仍未赋值, 先初始化
    headers = {
        'X-JLC-AccessToken': access_token,
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Html5Plus/1.0 (Immersed/20) JlcMobileApp',
    }

    for attempt in range(1, retries + 1):
        try:
            # 1. 获取金豆信息（先获取，用于获取 customer_code）
            bean_response = requests.get(gold_bean_url, headers=headers, timeout=15)
            bean_response.raise_for_status()
            bean_result = bean_response.json()

            # 获取 customerCode
            customer_code = bean_result['data']['customerCode']
            integral_voucher = bean_result['data']['integralVoucher']

            # 2. 执行签到请求
            sign_response = requests.get(url, headers=headers, timeout=15)
            sign_response.raise_for_status()
            sign_result = sign_response.json()

            # 检查签到是否成功
            if not sign_result.get('success'):
                message = sign_result.get('message', '未知错误')
                if '已经签到' in message:
                    print(f"ℹ️ [账号{customer_code}] 今日已签到")
                    return ("already", None, customer_code)
                else:
                    print(f"❌ [账号{customer_code}] 签到失败 - {message}")
                    return ("fail", f"签到被拒: {message}", customer_code)

            # 解析签到数据
            data = sign_result.get('data', {})

            # 安全地获取 gainNum 和 status
            gain_num = data.get('gainNum') if data else None
            status = data.get('status') if data else None

            # 处理签到结果
            if status and status > 0:
                if gain_num is not None and gain_num != 0:
                    print(f"✅ [账号{customer_code}] 今日签到成功")
                    return ("ok",
                            f"✅ 账号({customer_code})：获取{gain_num}个金豆，当前总数：{integral_voucher + gain_num}",
                            customer_code)
                else:
                    # 第七天特殊处理
                    seventh_response = requests.get(seventh_day_url, headers=headers, timeout=15)
                    seventh_response.raise_for_status()
                    seventh_result = seventh_response.json()

                    if seventh_result.get("success"):
                        print(f"🎉 [账号{customer_code}] 第七天签到成功")
                        return ("ok",
                                f"🎉 账号({customer_code})：第七天签到成功，当前金豆总数：{integral_voucher + 8}",
                                customer_code)
                    else:
                        print(f"ℹ️ [账号{customer_code}] 第七天签到失败，无金豆获取")
                        return ("already", None, customer_code)
            else:
                print(f"ℹ️ [账号{customer_code}] 今日已签到或签到失败")
                return ("already", None, customer_code)

        except RequestException as e:
            print(f"⚠️ [账号{mask_account(access_token)}] 第{attempt}次请求失败: {str(e)}")
            # 401/403 是 token 失效, 重试无用, 立即返回 (省 30 秒白等)
            resp = getattr(e, 'response', None)
            if resp is not None and getattr(resp, 'status_code', None) in (401, 403):
                reason = f"token 失效(HTTP {resp.status_code})，需重新抓包"
                print(f"❌ [账号{mask_account(access_token)}] {reason}")
                return ("fail", reason, customer_code)
            if attempt < retries:
                wait = backoff * attempt
                print(f"⏳ {wait} 秒后重试...")
                time.sleep(wait)
            else:
                print(f"❌ [账号{mask_account(access_token)}] 已重试 {retries} 次，放弃")
                return ("fail", "网络请求失败: " + str(e).split('(')[0].strip()[:80], customer_code)
        except KeyError as e:
            print(f"❌ [账号{mask_account(access_token)}] 数据解析失败: 缺少键 {str(e)}")
            return ("fail", f"数据解析失败: 缺少键 {str(e)}", customer_code)
        except Exception as e:
            print(f"❌ [账号{mask_account(access_token)}] 未知错误: {str(e)}")
            return ("fail", f"未知错误: {type(e).__name__}", customer_code)


# ======== 每周资产周报 ========
# 每周第一次运行时强制查一次所有账号资产 (只读接口), 与上周快照对比算本周新增;
# 快照存 state["weekly"], 不落明文 token (仍用 token_key 指纹做 key)。
# 首周运行无上周快照 -> 本周新增显示 "—"; 查询失败 -> 显示 "查询失败"。
# 邮件正文不含合计行, 各账号独立展示。

def week_str():
    """当前 ISO 周, 如 2026-W49 (东八区)"""
    return datetime.now(TZ_CN).strftime('%G-W%V')


def query_assets(access_token, retries=2, backoff=10):
    """只查资产接口, 返回 (customerCode, integralVoucher); 失败返回 (None, None)"""
    headers = {
        'X-JLC-AccessToken': access_token,
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Html5Plus/1.0 (Immersed/20) JlcMobileApp',
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(gold_bean_url, headers=headers, timeout=15)
            resp.raise_for_status()
            d = resp.json()['data']
            return (d['customerCode'], d['integralVoucher'])
        except Exception as e:
            if attempt < retries:
                time.sleep(backoff * attempt)
    return (None, None)


def build_weekly_report(current, prev, week_key):
    """生成 (标题, 正文)。current/prev 为 {token_key: {customerCode, voucher}}"""
    iso_year, iso_week = week_key.split('-W')
    monday = datetime.strptime(f'{iso_year}-W{iso_week}-1', '%G-W%V-%u').date()
    sunday = monday + timedelta(days=6)
    period = f"{monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')}"

    lines = [
        "嘉立创金豆资产周报",
        f"统计周期：{period}（{week_key}）",
        "",
        f"{'账号':<16}{'金豆总额':>10}{'本周新增':>10}",
        "-" * 36,
    ]
    for key, info in current.items():
        code = info.get("customerCode") or mask_account(key)
        voucher = info.get("voucher")
        if voucher is None:
            lines.append(f"{code:<16}{'查询失败':>14}")
            continue
        old = (prev.get(key) or {}).get("voucher")
        if old is None:
            delta = "—"
        else:
            diff = voucher - old
            delta = f"+{diff}" if diff > 0 else str(diff)
        lines.append(f"{code:<16}{voucher:>12,}{delta:>10}")
    lines.append("")
    lines.append(f"生成时间：{datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M')}")
    return (f"嘉立创金豆资产周报（{period}）", "\n".join(lines))


# ======== 主函数 ========

def main():
    # 从 GitHub Secrets 获取配置
    AccessTokenList = [token.strip() for token in TOKEN_LIST.split(',') if token.strip()]
    SendKeyList = [key.strip() for key in SEND_KEY_LIST.split(',') if key.strip()]

    # 检查配置是否为空
    if not AccessTokenList:
        print("❌ 请设置 TOKEN_LIST")
        return

    if not SendKeyList:
        print("❌ 请设置 SEND_KEY_LIST")
        return

    # 确保长度一致
    min_length = min(len(AccessTokenList), len(SendKeyList))
    AccessTokenList = AccessTokenList[:min_length]
    SendKeyList = SendKeyList[:min_length]

    print(f"🔧 共发现 {min_length} 个账号需要签到")

    # 按 SendKey 分组
    task_groups = defaultdict(list)
    for access_token, send_key in zip(AccessTokenList, SendKeyList):
        task_groups[send_key].append(access_token)

    print(f"📊 共分为 {len(task_groups)} 个通知组")

    # 当日签到记忆
    state = load_state()
    today = today_str()
    state_changed = False

    # 每周资产周报: 本周第一次运行时强制查一次所有账号资产 (独立于签到记忆)
    current_week = week_str()
    weekly = state.setdefault("weekly", {})
    if weekly.get("last_week") != current_week:
        print(f"\n📅 本周首次运行，查询 {len(AccessTokenList)} 个账号资产用于周报")
        prev_snapshot = weekly.get("snapshots") or {}
        current_snapshot = {}
        for j, token in enumerate(AccessTokenList):
            code, voucher = query_assets(token)
            current_snapshot[token_key(token)] = {
                "customerCode": code,
                "voucher": voucher,
            }
            if voucher is not None:
                print(f"   ℹ️ [账号{code}] 金豆 {voucher}")
            else:
                print(f"   ⚠️ [账号{mask_account(token)}] 资产查询失败")
            if j < len(AccessTokenList) - 1:
                lo, hi = (8, 20) if os.environ.get('GITHUB_ACTIONS') == 'true' else (3, 8)
                time.sleep(round(random.uniform(lo, hi), 1))
        title, body = build_weekly_report(current_snapshot, prev_snapshot, current_week)
        notify_email(title, body, os.getenv("MAIL_TO") or DEFAULT_MAIL_TO)
        weekly["last_week"] = current_week
        weekly["snapshots"] = current_snapshot
        state_changed = True
        print(f"✅ 周报已处理（周 key: {current_week}）")
    else:
        print(f"\n📅 本周周报已发送过（周 key: {current_week}），跳过")

    # 顺序执行签到任务
    group_results = {}
    failed_accounts = []  # 失败账号, 供邮件通知使用 (Server 酱通道不受影响)

    for send_key, tokens in task_groups.items():
        print(f"\n🚀 开始处理 SendKey: {send_key[:5]}... 的 {len(tokens)} 个账号")
        results = []

        for i, token in enumerate(tokens):
            print(f"📝 处理第 {i+1}/{len(tokens)} 个账号...")

            key = token_key(token)

            # 当日已签到 -> 直接跳过, 零请求 (防风控)
            done = state["days"].get(today, {}).get(key)
            if done:
                # 兼容旧格式 (值是字符串) 与新格式 (值是 dict 含客编)
                if isinstance(done, dict):
                    done_status = done.get("status")
                    done_code = done.get("customerCode") or mask_account(token)
                else:
                    done_status, done_code = done, mask_account(token)
                print(f"⏭️ [账号{done_code}] 今日已处理 (记忆: {done_status}), 跳过")
                continue

            # 执行签到
            status, message, customer_code = sign_in(token)
            if status == "ok" and message is not None:
                results.append(message)
            elif status == "fail":
                failed_accounts.append((customer_code or mask_account(token), message))

            # 成功/已签到 -> 写入当日记忆 (含完整客编), 今天再跑跳过行可显示; 失败不记, 下周期重试
            if status in ("ok", "already"):
                mark_done(state, today, key, status, customer_code)
                state_changed = True

            # 如果不是最后一个账号，则等待随机时间 (防风控: 固定节奏是机器特征)
            # ok 间隔大 (真签到动作); already 间隔短 (仅两个只读接口); fail 不等。
            if status in ("ok", "already") and i < len(tokens) - 1:
                import os as _os
                gha = _os.environ.get('GITHUB_ACTIONS') == 'true'
                if status == "ok":
                    lo, hi = (45, 150) if gha else (10, 30)
                else:  # already
                    lo, hi = (8, 20) if gha else (3, 8)
                wait_time = round(random.uniform(lo, hi), 1)
                print(f"⏳ 随机等待 {wait_time}s ({lo}~{hi}s) 后处理下一个账号...")
                time.sleep(wait_time)

        group_results[send_key] = results

    # 邮件通知 - 只在有账号失败时发送; 同一天同一失败集合只发一次
    # 与 Server 酱通道独立: 邮件报失败, Server 酱报金豆, 互不干扰
    if failed_accounts:
        # 去重键含原因: 同一账号同一原因当天只发一次; 原因变化 (如网络->401) 会再发
        reason = "|".join(sorted(set(f"{a}:{r}" for a, r in failed_accounts)))
        notified = state.setdefault("notified", {})
        if notified.get(today) == reason:
            print("ℹ️ 今日已就同一失败结论发过邮件，本轮不重复发送")
        else:
            body = ("以下账号签到失败，请及时处理：\n\n"
                    + "\n".join(f"- {a}：{r}" for a, r in sorted(set(failed_accounts))))
            notify_email("嘉立创签到异常", body, os.getenv("MAIL_TO") or DEFAULT_MAIL_TO)
            notified[today] = reason
            state_changed = True

    if state_changed:
        save_state(state)

    # 推送通知 - 只在有获取到金豆时才发送
    print("\n📬 开始检查是否需要发送通知...")
    notification_sent = False

    for send_key, results in group_results.items():
        if results:
            content = "\n\n".join(results)
            print(f"📤 检测到有金豆获取，准备发送通知给 SendKey: {send_key[:5]}...")

            response = send_msg_by_server(send_key, "嘉立创签到汇总", content)

            if response and response.get('code') == 0:
                print(f"✅ 通知发送成功！消息ID: {response.get('data', {}).get('pushid', '')}")
                notification_sent = True
            else:
                error_msg = response.get('message') if response else '网络异常或推送服务无响应'
                print(f"❌ 通知发送失败！错误: {error_msg}")
        else:
            print(f"⏭️ SendKey: {send_key[:5]}... 组内无金豆获取，跳过通知")

    if not notification_sent:
        print("ℹ️ 所有账号均未获取到金豆，无通知发送")


# ======== 程序入口 ========

if __name__ == '__main__':
    print("🏁 嘉立创自动签到任务开始")
    main()
    print("🏁 任务执行完毕")
