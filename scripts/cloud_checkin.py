#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 云端每日签到（GitHub Actions / 任意 cron 平台）

设计目标：在没有本机登录态、没有持久盘的云环境里，仅凭一个长效
refresh_token 完成每日签到。纯 Python 标准库，无第三方依赖，Python 3.8+。

链路（与 WorkBuddy 积分助手「通道 C」完全一致，只调用这三个接口）：
  1. POST {REFRESH_URL}
     用长效令牌换短效接口令牌；header: X-Refresh-Token / X-Auth-Refresh-Source / X-Domain
  2. POST {BILLING_BASE}/billing/meter/checkin-activity-status
     只读查询今日状态与积分余额
  3. POST {BILLING_BASE}/billing/meter/daily-checkin
     当日未签时领取（幂等；当日已签返回 code 10001）

执行结束后，可选把签到日报推送到 WxPusher（POST /api/send/message），
主题为「WorkBuddy 签到推送 + UTC+8 日期 + 空格 + 是否成功」，如
「WorkBuddy 签到推送 11月11日 周三 成功」。

安全边界（硬规则）：
  - 凭据只从环境变量 WORKBUDDY_REFRESH_TOKEN 读取；任何输出（文字 / JSON /
    GitHub Step Summary / 异常信息）都不含令牌明文，也不含脱敏片段。
  - 只调上述三个接口；不调用兑换、抽奖、旅行等任何其他写接口。
  - 接口令牌只在内存中使用；轮换出的新长效令牌仅在显式设置
    WORKBUDDY_RT_OUT 时写入该文件（0600），由工作流自行回写 Secret。
  - 「先落盘新令牌，再执行签到」：即使签到失败也不丢失轮换结果。
  - WxPusher appToken / UID 只作为请求体字段发送，绝不出现在输出、日志或摘要里。

环境变量：
  WORKBUDDY_REFRESH_TOKEN  必填，长效 refresh_token
  WORKBUDDY_RT_OUT         可选，轮换后新令牌的落盘路径（供回写 Secret）
  WORKBUDDY_REFRESH_URL    可选，覆盖刷新端点（自测 / 代理用）
  WORKBUDDY_BILLING_BASE   可选，覆盖签到基址，默认 https://www.codebuddy.cn/v2
  GITHUB_STEP_SUMMARY      Actions 自动注入；脚本会追加一段运行摘要

WxPusher 日报推送（可选；凭据只从环境变量读，绝不写进代码 / 日志）：
  WXPUSHER_APP_TOKEN       启用推送时必填：应用 appToken
  WXPUSHER_UID             接收者 UID，多个用逗号分隔；与 TOPIC_ID 二选一
  WXPUSHER_TOPIC_ID        接收主题 ID（数字），多个用逗号分隔
  WXPUSHER_ALWAYS          设为 1 时「今日已签」也推送（默认去重，每天一条）
  WXPUSHER_API_URL         可选，覆盖推送端点（本地自测用）

命令行：
  python cloud_checkin.py               人类可读摘要
  python cloud_checkin.py --json        JSON 摘要（便于自动化读取）
  python cloud_checkin.py --check-only  只验证链路，不执行签到（不触发写接口）

退出码：0 成功（含「今日已签」）；1 运行失败；2 未配置令牌
"""

import html as _html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REFRESH_URL = (os.environ.get("WORKBUDDY_REFRESH_URL")
               or "https://copilot.tencent.com/v2/plugin/auth/token/refresh")
BILLING_BASE = (os.environ.get("WORKBUDDY_BILLING_BASE")
                or "https://www.codebuddy.cn/v2").rstrip("/")
WXPUSHER_API_URL = (os.environ.get("WXPUSHER_API_URL")
                    or "https://wxpusher.zjiecode.com/api/send/message")
TITLE_PREFIX = "WorkBuddy 签到推送"
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"

HTTP_TIMEOUT = 15
MAX_RETRY = 2
USER_AGENT = "WorkBuddy-Checkin-Script/1.1"

BALANCE_KEYS = (
    "total_credits", "total_credit", "total_credit_balance", "total_points",
    "balance", "credits", "credit_balance", "remain_credit", "score",
    "totalCredit", "totalCredits", "pointsBalance",
)


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"raw": (raw or b"").decode("utf-8", "replace")[:200]}


def _request(url, headers=None, payload=None):
    """POST JSON；HTTP 错误也返回响应体（已签到等场景会返回 4xx + 业务码）。

    仅对网络类异常重试；业务错误直接返回，交给调用方判定。
    """
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    last_err = ""
    for attempt in range(MAX_RETRY + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, _parse(exc.read())
            except Exception:
                return exc.code, {"raw": ""}
        except Exception as exc:
            last_err = str(exc)[:200]
            if attempt < MAX_RETRY:
                time.sleep(2)
    return 0, {"raw": last_err or "network error"}


def _write_rt(path, token):
    """把轮换出的长效令牌写入临时文件（0600），供工作流回写 Secret。"""
    if not path or not token:
        return False
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token.strip())
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _extract_balance(*bodies):
    for body in bodies:
        if not isinstance(body, dict):
            continue
        nested = body.get("data")
        nodes = [body, nested]
        if isinstance(nested, dict):
            nodes.append(nested.get("result"))
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in BALANCE_KEYS:
                value = node.get(key)
                if isinstance(value, (int, float)):
                    return value
    return None


def _today_signed(body):
    if not isinstance(body, dict):
        return False
    if str(body.get("code", "")) == "10001":
        return True
    if body.get("today_checked_in") is True:
        return True
    data = body.get("data")
    if isinstance(data, dict):
        if data.get("today_checked_in") is True:
            return True
        if str(data.get("code", "")) == "10001":
            return True
    return False


def _cn_now():
    """固定 UTC+8 的当前时间，不依赖容器本地时区。"""
    return datetime.now(timezone(timedelta(hours=8)))


def _date_label():
    """日报标题用日期：月日 + 星期（UTC+8），如「11月11日 周三」。"""
    now = _cn_now()
    weekday = "一二三四五六日"[now.weekday()]
    return "%d月%d日 周%s" % (now.month, now.day, weekday)


def _build_daily_report(result):
    """构造 WxPusher 日报：summary 是通知栏标题，content 是 HTML 正文。

    主题（summary）格式：前缀 + UTC+8 日期 + 空格 + 是否成功，
    如「WorkBuddy 签到推送 11月11日 周三 成功」。
    正文抬头与主题一致，正文不含任何令牌信息。
    """
    label = _date_label()
    timestamp = _cn_now().strftime("%Y-%m-%d %H:%M")
    ok = result.get("status") == "ok"
    outcome = "成功" if ok else "失败"
    summary = "%s %s %s" % (TITLE_PREFIX, label, outcome)
    color = "#1a7f37" if ok else "#c62828"

    rows = [
        ("结果", result.get("msg") or "-"),
        ("状态", "%s / %s" % (result.get("status"), result.get("action"))),
    ]
    if result.get("points") is not None:
        rows.append(("本次积分", "+%s" % result["points"]))
    if result.get("balance") is not None:
        rows.append(("积分余额", result["balance"]))
    if result.get("streak_days") is not None:
        rows.append(("连续签到", "%s 天" % result["streak_days"]))
    rows.append(("长效令牌轮换", "是" if result.get("rt_rotated") else "否"))

    lines = [
        "<h2>%s</h2>" % summary,
        "<p>%s（UTC+8）｜<b style=\"color:%s\">%s</b></p>"
        % (timestamp, color, "执行成功" if ok else "执行失败"),
        "<table cellpadding=\"4\">",
    ]
    for key, value in rows:
        lines.append("<tr><td>%s</td><td>%s</td></tr>"
                     % (_html.escape(key), _html.escape(str(value))))
    lines.append("</table>")
    lines.append("<p style=\"color:#888\">由 GitHub Actions 自动推送</p>")
    return summary, "".join(lines)


def _push_wxpusher(result):
    """把签到日报推送到 WxPusher。配置缺失或推送失败都不影响签到结果。

    幂等双跑时「今日已签」默认不再重复推送；设 WXPUSHER_ALWAYS=1 可关闭去重。
    """
    info = {"enabled": False, "ok": None, "reason": ""}
    token = (os.environ.get("WXPUSHER_APP_TOKEN") or "").strip()
    if not token:
        info["reason"] = "未配置 WXPUSHER_APP_TOKEN，跳过推送"
        return info
    info["enabled"] = True

    uids = [item.strip() for item in
            (os.environ.get("WXPUSHER_UID") or "").split(",") if item.strip()]
    topic_ids = []
    for raw in (os.environ.get("WXPUSHER_TOPIC_ID") or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            topic_ids.append(int(raw))
    if not uids and not topic_ids:
        info.update(ok=False,
                    reason="缺少接收者：需配置 WXPUSHER_UID 或 WXPUSHER_TOPIC_ID")
        return info

    always = (os.environ.get("WXPUSHER_ALWAYS") or "").strip().lower()
    if (result.get("status") == "ok"
            and result.get("action") == "skip_already_signed"
            and always not in ("1", "true", "yes")):
        info["reason"] = "今日已签到，跳过重复日报"
        return info

    summary, content = _build_daily_report(result)
    payload = {
        "appToken": token,
        "content": content,
        "summary": summary,
        "contentType": 2,
    }
    if uids:
        payload["uids"] = uids
    if topic_ids:
        payload["topicIds"] = topic_ids

    try:
        request = urllib.request.Request(
            WXPUSHER_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = _parse(resp.read())
        if isinstance(body, dict) and (str(body.get("code")) == "1000"
                                       or body.get("success") is True):
            info.update(ok=True, reason="已推送：" + summary)
        else:
            code = body.get("code") if isinstance(body, dict) else "-"
            info.update(ok=False, reason="接口返回异常 code=%s" % code)
    except urllib.error.HTTPError as exc:
        info.update(ok=False, reason="推送 HTTP %s" % exc.code)
    except Exception as exc:
        info.update(ok=False, reason="推送异常：%s" % str(exc)[:120])
    return info


def _human(result):
    lines = [
        "状态: %s" % result.get("status"),
        "结果: %s" % result.get("msg", ""),
    ]
    if result.get("points") is not None:
        lines.append("本次积分: +%s" % result["points"])
    if result.get("balance") is not None:
        lines.append("积分余额: %s" % result["balance"])
    if result.get("streak_days") is not None:
        lines.append("连续签到: %s 天" % result["streak_days"])
    lines.append("长效令牌轮换: %s" % ("是" if result.get("rt_rotated") else "否"))
    if result.get("rt_rotated"):
        lines.append("新令牌落盘: %s" % (
            "是（待工作流回写 Secret）" if result.get("rt_out_written")
            else "否（未设置 WORKBUDDY_RT_OUT）"))
    push = result.get("push") or {}
    if push.get("enabled"):
        mark = ("成功" if push.get("ok") else
                "跳过" if push.get("ok") is None else "失败")
        lines.append("日报推送: %s（%s）" % (mark, push.get("reason", "")))
    else:
        lines.append("日报推送: 未启用（%s）" % push.get("reason", ""))
    return "\n".join(lines)


def _finish(result, as_json, exit_code):
    result["push"] = _push_wxpusher(result)
    text = _human(result)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("## WorkBuddy 每日签到\n\n```\n" + text + "\n```\n")
        except Exception:
            pass
    return exit_code


def main():
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    as_json = "--json" in argv
    check_only = "--check-only" in argv

    result = {
        "status": "unknown",
        "action": None,
        "points": None,
        "balance": None,
        "streak_days": None,
        "msg": "",
        "rt_rotated": False,
        "rt_out_written": False,
    }

    refresh_token = (os.environ.get("WORKBUDDY_REFRESH_TOKEN") or "").strip()
    if not refresh_token:
        result.update(
            status="need_setup",
            msg="未配置 WORKBUDDY_REFRESH_TOKEN（长效令牌）；请在仓库 Secrets 中配置",
        )
        return _finish(result, as_json, 2)

    # 1) 长效令牌 -> 短效接口令牌
    _, refresh_body = _request(REFRESH_URL, headers={
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
        "X-Domain": "copilot.tencent.com",
    })
    data = refresh_body.get("data") if isinstance(refresh_body, dict) else None
    code = str(refresh_body.get("code", "")) if isinstance(refresh_body, dict) else "-"
    if not isinstance(data, dict) or code not in ("0", "200", ""):
        detail = ""
        if isinstance(refresh_body, dict):
            detail = refresh_body.get("msg") or refresh_body.get("message") or ""
        result.update(
            status="error",
            action="refresh_failed",
            msg="令牌刷新失败（code=%s %s）；长效令牌可能已失效，请重新导出并更新 Secret"
                % (code, detail),
        )
        return _finish(result, as_json, 1)

    access_token = data.get("accessToken") or ""
    rotated_token = data.get("refreshToken") or ""
    if not access_token:
        result.update(status="error", action="refresh_failed",
                      msg="刷新应答缺少接口令牌，请稍后重试")
        return _finish(result, as_json, 1)

    # 2) 立刻落盘轮换结果：后续签到失败也不能丢令牌
    rotated = bool(rotated_token and rotated_token != refresh_token)
    result["rt_rotated"] = rotated
    if rotated:
        result["rt_out_written"] = _write_rt(
            os.environ.get("WORKBUDDY_RT_OUT"), rotated_token)

    auth = {"Authorization": "Bearer %s" % access_token}

    # 3) 只读状态查询。若状态接口偶发异常，仍继续走签到接口——它是幂等的，
    #    比因查询失败而漏签更安全。
    status_http, status_body = _request(BILLING_BASE + STATUS_PATH, headers=auth)
    result["balance"] = _extract_balance(status_body)
    result["status_http"] = status_http

    if _today_signed(status_body):
        result.update(status="ok", action="skip_already_signed",
                      msg="今日已签到，无需重复领取")
        return _finish(result, as_json, 0)

    if check_only:
        result.update(status="ok", action="skip_check_only",
                      msg="链路验证成功（未执行签到，未调用任何写接口）")
        return _finish(result, as_json, 0)

    # 4) 签到（幂等）
    checkin_http, checkin_body = _request(BILLING_BASE + CHECKIN_PATH, headers=auth)
    result["checkin_http"] = checkin_http

    if isinstance(checkin_body, dict):
        biz_code = str(checkin_body.get("code", ""))
        biz_msg = checkin_body.get("msg") or checkin_body.get("message") or ""
        payload = checkin_body.get("data") \
            if isinstance(checkin_body.get("data"), dict) else {}

        if biz_code == "10001" or "已签到" in biz_msg:
            result.update(status="ok", action="skip_already_signed",
                          msg="今日已签到（接口返回幂等提示）")
            return _finish(result, as_json, 0)

        if 200 <= checkin_http < 300 and biz_code in ("", "0", "200"):
            credit = (checkin_body.get("credit") or payload.get("credit")
                      or payload.get("daily_credit") or payload.get("today_credit"))
            streak = checkin_body.get("streak_days") or payload.get("streak_days")
            balance = _extract_balance(checkin_body)
            if balance is not None:
                result["balance"] = balance
            result.update(
                status="ok", action="clicked", points=credit, streak_days=streak,
                msg="签到成功" + (("，+%s 积分" % credit) if credit else "")
                    + (("，连续第 %s 天" % streak) if streak else ""),
            )
            return _finish(result, as_json, 0)

        result.update(status="error", action="failed",
                      msg=biz_msg or ("HTTP %s（业务码 %s）" % (checkin_http, biz_code)))
    else:
        result.update(status="error", action="failed",
                      msg="签到接口返回非 JSON 内容")

    return _finish(result, as_json, 1)


if __name__ == "__main__":
    sys.exit(main())
