"""12306 访问层：会话、站点映射、余票查询。

单独成模块的原因：`a.py` 是 Streamlit 页面，一 import 就会执行整页逻辑，
所以任何要复用的能力（盯票 Agent、测试脚本、命令行工具）都不能直接 import a.py。
这里只做「取数据」，不含任何 UI 或 Agent 逻辑。
"""
import json
import time
from pathlib import Path

import certifi
import requests

BASE_DIR = Path(__file__).parent
STATION_CACHE = BASE_DIR / "stations.json"

SKIP_SSL = False
try:  # 兼容 app 层用环境变量关闭证书校验的做法
    import os

    SKIP_SSL = os.getenv("SKIP_SSL_VERIFY", "0") == "1"
except Exception:  # pragma: no cover
    pass
VERIFY = False if SKIP_SSL else certifi.where()

if SKIP_SSL:
    # 12306 的证书链在容器里校验不过（缺中间证书），只能关校验。
    # 关掉之后 requests 会给**每个请求**打两行 InsecureRequestWarning，
    # 24 小时盯票就是几万行日志，真正的事件被淹掉。
    # 这里只关「这条已知的告警」，不是关掉整个 warnings —— 别的警告还要能看见。
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 12306 席别代码 → 名称
SEAT_CODE_MAP = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "W": "软卧", "4": "软卧", "3": "硬卧", "1": "硬座",
    "A": "高级软卧", "F": "动卧", "C": "软座",
}

# 余票字段在 | 分隔串里的下标
REMAIN_INDEX = {
    "商务座": 29, "一等座": 30, "二等座": 31,
    "软卧": 28, "硬卧": 26, "硬座": 24, "无座": 23,
}

_session = None
_station_map = None


def parse_prices(seat_types: str, price_str: str) -> dict:
    prices = {}
    if not price_str:
        return prices
    for i, code in enumerate(seat_types):
        start, end = i * 10, i * 10 + 10
        if end > len(price_str):
            break
        digits = price_str[start + 1:end]
        try:
            prices[SEAT_CODE_MAP.get(code, code)] = int(digits[:5]) / 10.0
        except (ValueError, IndexError):
            continue
    return prices


def get_session(force_refresh: bool = False) -> requests.Session:
    global _session
    if _session is None or force_refresh:
        _session = requests.Session()
        _session.headers.update(HEADERS)
        try:
            _session.get("https://kyfw.12306.cn/otn/leftTicket/init",
                         timeout=15, verify=VERIFY)
        except Exception:
            pass
    return _session


def get_station_map() -> dict:
    global _station_map
    if _station_map is not None:
        return _station_map
    if STATION_CACHE.exists():
        _station_map = json.loads(STATION_CACHE.read_text(encoding="utf-8"))
        return _station_map
    s = get_session()
    r = s.get(
        "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
        timeout=15, verify=VERIFY,
    )
    mapping = {}
    for chunk in r.text.split("@"):
        parts = chunk.split("|")
        if len(parts) >= 3:
            name, code = parts[1], parts[2]
            if name and code:
                mapping[name] = code
    STATION_CACHE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    _station_map = mapping
    return mapping


def get_city_stations(name: str) -> list:
    """输入城市名，返回该城市所有火车站（前缀匹配，含主站+东南西北等站）。"""
    station_map = get_station_map()
    name = name.strip()
    matched = [s for s in station_map.keys() if s.startswith(name)]
    return matched if matched else [name]


def query_ticket_dict(start_station: str, end_station: str, travel_date: str) -> dict:
    """查余票，返回结构化 dict：{"trains": [...]} 或 {"error": "..."}。"""
    station_map = get_station_map()
    from_code = station_map.get(start_station.strip())
    to_code = station_map.get(end_station.strip())

    if not from_code:
        return {"error": f"找不到出发站「{start_station}」"}
    if not to_code:
        return {"error": f"找不到到达站「{end_station}」"}

    for attempt in range(2):
        s = get_session(force_refresh=(attempt == 1))
        try:
            r = s.get(
                "https://kyfw.12306.cn/otn/leftTicket/query",
                params={
                    "leftTicketDTO.train_date": travel_date,
                    "leftTicketDTO.from_station": from_code,
                    "leftTicketDTO.to_station": to_code,
                    "purpose_codes": "ADULT",
                },
                timeout=15, verify=VERIFY,
            )
            data = r.json()
        except Exception as e:
            if attempt == 0:
                continue
            return {"error": f"网络请求异常：{e}"}

        if not data.get("status"):
            if attempt == 0:
                time.sleep(0.5)
                continue
            return {"error": f"查询失败：{data.get('messages', '未知错误')}"}

        results = data["data"]["result"]
        name_map = data["data"]["map"]

        # 空结果也可能是限流，重试一次
        if not results and attempt == 0:
            time.sleep(0.5)
            continue

        trains = []
        for row in results:
            f = row.split("|")
            seats = {}
            for seat_name, idx in REMAIN_INDEX.items():
                remain = f[idx].strip() if idx < len(f) else ""
                if remain:
                    seats[seat_name] = {"remain": remain}
            prices = parse_prices(f[35], f[39])
            for seat_name, price in prices.items():
                seats.setdefault(seat_name, {})["price"] = price

            trains.append({
                "train_no": f[3],
                "departure_station": name_map.get(f[6], f[6]),
                "arrival_station": name_map.get(f[7], f[7]),
                "departure_time": f[8],
                "arrival_time": f[9],
                "duration": f[10],
                "seats": seats,
            })

        return {"trains": trains}

    return {"error": "查询失败，请重试"}


def query_train_ticket(start_station: str, end_station: str, travel_date: str) -> str:
    """查询站到站火车票余票和票价（JSON 字符串，供 Agent 工具调用）。"""
    return json.dumps(query_ticket_dict(start_station, end_station, travel_date),
                      ensure_ascii=False)
