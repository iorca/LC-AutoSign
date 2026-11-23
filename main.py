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
                    return ("fail", None, customer_code)

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
            if attempt < retries:
                wait = backoff * attempt
                print(f"⏳ {wait} 秒后重试...")
                time.sleep(wait)
            else:
                print(f"❌ [账号{mask_account(access_token)}] 已重试 {retries} 次，放弃")
                return ("fail", None, customer_code)
        except KeyError as e:
            print(f"❌ [账号{mask_account(access_token)}] 数据解析失败: 缺少键 {str(e)}")
            return ("fail", None, customer_code)
        except Exception as e:
            print(f"❌ [账号{mask_account(access_token)}] 未知错误: {str(e)}")
            return ("fail", None, customer_code)


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

    # 顺序执行签到任务
    group_results = {}

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

            # 成功/已签到 -> 写入当日记忆 (含完整客编), 今天再跑跳过行可显示; 失败不记, 下周期重试
            if status in ("ok", "already"):
                mark_done(state, today, key, status, customer_code)
                state_changed = True

            # 如果不是最后一个账号，则等待随机时间
            if i < len(tokens) - 1:
                # 随机间隔 (防风控: 固定节奏是机器特征); GHA 无人值守加大到 45~150s
                import os as _os
                lo, hi = (45, 150) if _os.environ.get('GITHUB_ACTIONS') == 'true' else (10, 30)
                wait_time = round(random.uniform(lo, hi), 1)
                print(f"⏳ 随机等待 {wait_time}s ({lo}~{hi}s) 后处理下一个账号...")
                time.sleep(wait_time)

        group_results[send_key] = results

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
