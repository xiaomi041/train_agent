"""出票卡片：把「命中」变成「下一步只差点击」。

为什么不做自动下单：
- 12306 用户协议禁止第三方工具自动购票，账号会被风控（封的是用户本人的实名账号）；
- 真下单需要登录票据 + 短信/滑块 + 支付密码，把支付凭证放进这个项目等于自己开洞。

所以这里做的是**半自动里最安全的一档**：
1. 生成 12306 官方查票深链（直达该日期、该起终点的查询页），点开只剩「预订」一步；
2. 生成一段可复制的文字（车次、日期、席别、价格、换乘点），方便贴给自己或同伴。

深链参数说明（12306 网页版自己在用的那套）：
    fs/ts  出发/到达城市的车站列表，英文逗号分隔
    date   乘车日期 YYYY-MM-DD
    linktypeid=dc  单程
"""
from __future__ import annotations

from urllib.parse import quote

BASE = "https://kyfw.12306.cn/otn/leftTicket/init"


def search_link(dep: str, arr: str, date: str) -> str:
    """12306 官方查票深链（只筛到城市 + 日期，具体车次由用户点「预订」）。"""
    if not dep or not arr or not date:
        return ""
    return (
        f"{BASE}?linktypeid=dc&fs={quote(dep)}&ts={quote(arr)}"
        f"&date={quote(date)}&flag=N,N,Y"
    )


def card_text(scheme) -> str:
    """一段可直接复制到聊天窗口的出票信息。"""
    if scheme is None:
        return ""
    lines = [
        f"🚄 {scheme.date} {scheme.dep_station} → {scheme.arr_station}",
        f"车次：{scheme.train_no}",
        f"时间：{scheme.dep_time} - {scheme.arr_time}（约 {scheme.duration_min // 60}h{scheme.duration_min % 60:02d}m）",
        f"席别：{scheme.seat}　价格：¥{scheme.price:.0f}",
    ]
    if getattr(scheme, "transfers", 0):
        lines.append(f"换乘：{scheme.transfers} 次（{scheme.via}）")
    lines.append(f"查票：{search_link(scheme.dep_station, scheme.arr_station, scheme.date)}")
    return "\n".join(lines)


def one_line(scheme) -> str:
    """通知里用的一行摘要（带深链，手机上点得动）。"""
    if scheme is None:
        return ""
    link = search_link(scheme.dep_station, scheme.arr_station, scheme.date)
    return (
        f"{scheme.date} {scheme.train_no} {scheme.dep_station}→{scheme.arr_station} "
        f"{scheme.dep_time}-{scheme.arr_time} {scheme.seat} ¥{scheme.price:.0f}\n{link}"
    )
