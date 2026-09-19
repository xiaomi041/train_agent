"""
AI 火车票查询助手（12306 官方接口版）
- 数据：12306 官方余票查询接口（免费、无限制）
- AI：DeepSeek + Agno Agent
- 界面：Streamlit
"""
import os
import json
import requests
import certifi
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import streamlit as st
from agno.agent import Agent
from agno.models.deepseek import DeepSeek

# ========== 配置 ==========
load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
SKIP_SSL = os.getenv("SKIP_SSL_VERIFY", "0") == "1"
VERIFY = False if SKIP_SSL else certifi.where()

STATION_CACHE = Path(__file__).parent / "stations.json"
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

# 余票字段索引（12306 固定位置）
REMAIN_INDEX = {
    "商务座": 29, "一等座": 30, "二等座": 31,
    "软卧": 28, "硬卧": 26, "硬座": 24, "无座": 23,
}


def parse_prices(seat_types: str, price_str: str) -> dict:
    """从 [35] 席别代码串和 [39] 价格串解析各席别票价（单位：元）。"""
    prices = {}
    if not price_str:
        return prices
    for i, code in enumerate(seat_types):
        start = i * 10
        end = start + 10
        if end > len(price_str):
            break
        digits = price_str[start + 1:end]
        try:
            price = int(digits[:5]) / 10.0
        except (ValueError, IndexError):
            continue
        name = SEAT_CODE_MAP.get(code, code)
        prices[name] = price
    return prices


# ========== 12306 会话与站点映射 ==========
def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get("https://kyfw.12306.cn/otn/leftTicket/init", timeout=15, verify=VERIFY)
    return s


@st.cache_resource(show_spinner=False)
def get_station_map() -> dict:
    """站点名称→三字码映射，优先读本地缓存。"""
    if STATION_CACHE.exists():
        return json.loads(STATION_CACHE.read_text(encoding="utf-8"))
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
    return mapping


# ========== 工具：查询站到站余票 ==========
def query_train_ticket(start_station: str, end_station: str, travel_date: str) -> str:
    """
    查询站到站火车票余票。
    :param start_station: 出发站中文名，如 "十堰东"
    :param end_station:   到达站中文名，如 "武汉"
    :param travel_date:   出行日期 YYYY-MM-DD
    """
    station_map = get_station_map()
    from_code = station_map.get(start_station.strip())
    to_code = station_map.get(end_station.strip())

    if not from_code:
        return json.dumps({"error": f"找不到出发站「{start_station}」"}, ensure_ascii=False)
    if not to_code:
        return json.dumps({"error": f"找不到到达站「{end_station}」"}, ensure_ascii=False)

    s = get_session()
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
        return json.dumps({"error": f"网络请求异常：{e}"}, ensure_ascii=False)

    if not data.get("status"):
        return json.dumps({"error": f"查询失败：{data.get('messages', '未知错误')}"},
                          ensure_ascii=False)

    results = data["data"]["result"]
    name_map = data["data"]["map"]

    trains = []
    for row in results:
        f = row.split("|")
        # 解析各席别余票
        seats = {}
        for seat_name, idx in REMAIN_INDEX.items():
            remain = f[idx].strip() if idx < len(f) else ""
            if remain:
                seats[seat_name] = {"remain": remain}
        # 解析各席别票价，合并到 seats
        prices = parse_prices(f[35], f[39])
        for seat_name, price in prices.items():
            if seat_name in seats:
                seats[seat_name]["price"] = price
            else:
                seats[seat_name] = {"price": price}

        trains.append({
            "train_no": f[3],
            "departure_station": name_map.get(f[6], f[6]),
            "arrival_station": name_map.get(f[7], f[7]),
            "departure_time": f[8],
            "arrival_time": f[9],
            "duration": f[10],
            "seats": seats,
        })

    return json.dumps({"trains": trains}, ensure_ascii=False)


# ========== 工具：中转方案计算 ==========
def calc_transfer_scheme(train1_json: str, train2_json: str) -> str:
    """
    传入两段查询结果，枚举所有可行中转组合，返回：
    - 二等座总价最低方案
    - 全程总时间最短方案（含中转等待）
    """
    t1 = json.loads(train1_json)
    t2 = json.loads(train2_json)
    if "error" in t1 or "error" in t2:
        return json.dumps({"schemes": [], "msg": "某一段查询失败"})

    def _second_price(train):
        return (train.get("seats", {}).get("二等座", {}) or {}).get("price")

    schemes = []
    for seg1 in t1["trains"]:
        try:
            arr1 = datetime.strptime(seg1["arrival_time"], "%H:%M")
            dep1 = datetime.strptime(seg1["departure_time"], "%H:%M")
        except Exception:
            continue
        p1 = _second_price(seg1)
        for seg2 in t2["trains"]:
            try:
                dep2 = datetime.strptime(seg2["departure_time"], "%H:%M")
                arr2 = datetime.strptime(seg2["arrival_time"], "%H:%M")
            except Exception:
                continue
            if arr1 >= dep2:
                continue
            wait_min = int((dep2 - arr1).total_seconds() / 60)
            total_min = int((arr2 - dep1).total_seconds() / 60)
            p2 = _second_price(seg2)
            total_price = (p1 + p2) if (p1 is not None and p2 is not None) else None

            schemes.append({
                "seg1": seg1,
                "seg2": seg2,
                "transfer_wait_min": wait_min,
                "total_minutes": total_min,
                "total_second_price": total_price,
            })

    by_price = sorted(schemes, key=lambda x: (x["total_second_price"] is None,
                                              x["total_second_price"] or 0))
    by_time = sorted(schemes, key=lambda x: x["total_minutes"])

    return json.dumps({
        "total_count": len(schemes),
        "cheapest": by_price[0] if by_price else None,
        "fastest": by_time[0] if by_time else None,
        "top5_by_price": by_price[:5],
        "top5_by_time": by_time[:5],
    }, ensure_ascii=False)


# ========== Streamlit 界面 ==========
st.set_page_config(page_title="AI 火车票查询", page_icon="🚄", layout="wide")
st.title("🚄 AI 火车票查询助手")
st.caption("数据源：12306 官方接口 · 免费无限次 · AI 帮你整理结果")

if not DEEPSEEK_API_KEY:
    st.error("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")
    st.stop()


@st.cache_resource
def get_agent():
    return Agent(
        model=DeepSeek(api_key=DEEPSEEK_API_KEY),
        tools=[query_train_ticket, calc_transfer_scheme],
        instructions="""你是火车票查询助手。

工具说明：
- query_train_ticket(start, end, date)：查询站到站车次，返回每趟车的车次号、出发到达站、出发到达时间、历时、各席别信息。每个席别包含 remain（余票，"有"或数字）和 price（票价，元）。
- calc_transfer_scheme(第一段json, 第二段json)：传入两段查询结果，枚举所有可行中转组合，返回 cheapest（二等座总价最低）、fastest（总时间最短）、top5_by_price、top5_by_time。

输出规则：
1. 直达查询：
   (a) 先放"全部车次"完整表格，列：车次、出发站、到达站、出发时间、到达时间、历时、然后每个有票席别一列，格式为"价格元/余票"（如"243元/有"或"221元/14张"）。必须列出所有车次，不要省略。
   (b) 表格后单独标出"🚀 最快"和"💰 二等座最便宜"分别是哪趟车。
2. 中转查询：
   (a) 先展示 cheapest（二等座总价最低）和 fastest（全程最快）两个方案，分别列出两段车次、出发到达时间、等待时间、总票价、总耗时。
   (b) 然后分别列出"A→中转站 全部车次"和"中转站→B 全部车次"两个完整表格，格式同直达查询。
3. 余票数字小于5的标注"⚠️紧张"。
4. 票价为0或缺失的席别不要显示。
5. 站名必须是12306官方站名（如"十堰东"不是"十堰"），查不到就提示用户确认。
6. 语言简洁。
""",
    )


agent = get_agent()

col1, col2, col3 = st.columns(3)
with col1:
    start = st.text_input("出发站", value="十堰东")
with col2:
    transfer = st.text_input("中转站（不需要就留空）", value="")
with col3:
    end = st.text_input("到达站", value="武汉")

date = st.date_input("出行日期", value=datetime.now().date())

if st.button("🔍 开始查询", type="primary", use_container_width=True):
    date_str = date.strftime("%Y-%m-%d")
    with st.spinner("AI 正在帮你查 12306..."):
        if transfer.strip():
            prompt = (
                f"帮我查 {start} → {transfer} → {end} 的中转方案，"
                f"出行日期 {date_str}。先分段查询，再计算中转组合。"
            )
        else:
            prompt = f"帮我查 {start} 到 {end}，{date_str} 的所有直达车次余票。"

        result = agent.run(prompt)
        st.markdown(result.content)
