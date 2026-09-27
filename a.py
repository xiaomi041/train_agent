#111111111111
"""
AI 火车票查询助手（12306 官方接口版）
- 数据：12306 官方余票查询接口（免费、无限制）
- AI：DeepSeek + Agno Agent（多轮对话 + 知识库 RAG）
- 界面：Streamlit 聊天式界面
"""
import os
import json
import time
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
KNOWLEDGE_FILE = Path(__file__).parent / "train_knowledge.md"
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

REMAIN_INDEX = {
    "商务座": 29, "一等座": 30, "二等座": 31,
    "软卧": 28, "硬卧": 26, "硬座": 24, "无座": 23,
}


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


# ========== 12306 会话与站点映射（全局单例，避免重复握手触发反爬） ==========
_session = None
_station_map = None


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


# ========== 工具：查询站到站余票 ==========
def query_train_ticket(start_station: str, end_station: str, travel_date: str) -> str:
    """查询站到站火车票余票和票价。session失效时自动重试一次。"""
    station_map = get_station_map()
    from_code = station_map.get(start_station.strip())
    to_code = station_map.get(end_station.strip())

    if not from_code:
        return json.dumps({"error": f"找不到出发站「{start_station}」"}, ensure_ascii=False)
    if not to_code:
        return json.dumps({"error": f"找不到到达站「{end_station}」"}, ensure_ascii=False)

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
            return json.dumps({"error": f"网络请求异常：{e}"}, ensure_ascii=False)

        if not data.get("status"):
            if attempt == 0:
                time.sleep(0.5)
                continue
            return json.dumps({"error": f"查询失败：{data.get('messages', '未知错误')}"},
                              ensure_ascii=False)

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

        return json.dumps({"trains": trains}, ensure_ascii=False)

    return json.dumps({"error": "查询失败，请重试"}, ensure_ascii=False)


# ========== 工具：中转方案计算 ==========
def calc_transfer_scheme(train1_json: str, train2_json: str) -> str:
    """枚举所有中转组合，返回最便宜和最快方案。"""
    t1 = json.loads(train1_json)
    t2 = json.loads(train2_json)
    if "error" in t1 or "error" in t2:
        return json.dumps({"schemes": [], "msg": "某一段查询失败"})

    def _min_price(train):
        """取所有席别中最低的有票价格"""
        seats = train.get("seats", {})
        prices = [info["price"] for info in seats.values() if info.get("price") and info["price"] > 0]
        return min(prices) if prices else None

    schemes = []
    for seg1 in t1["trains"]:
        try:
            dep1 = datetime.strptime(seg1["departure_time"], "%H:%M")
            arr1 = datetime.strptime(seg1["arrival_time"], "%H:%M")
            if arr1 < dep1:
                arr1 = arr1.replace(day=arr1.day + 1)
        except Exception:
            continue
        p1 = _min_price(seg1)
        for seg2 in t2["trains"]:
            try:
                dep2 = datetime.strptime(seg2["departure_time"], "%H:%M")
                arr2 = datetime.strptime(seg2["arrival_time"], "%H:%M")
                if arr2 < dep2:
                    arr2 = arr2.replace(day=arr2.day + 1)
                if dep2 < arr1:
                    dep2 = dep2.replace(day=dep2.day + 1)
                    arr2 = arr2.replace(day=arr2.day + 1)
            except Exception:
                continue
            if arr1 > dep2:
                continue
            wait_min = int((dep2 - arr1).total_seconds() / 60)
            total_min = int((arr2 - dep1).total_seconds() / 60)
            p2 = _min_price(seg2)
            total_price = (p1 + p2) if (p1 is not None and p2 is not None) else None
            schemes.append({
                "seg1": seg1, "seg2": seg2,
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


# ========== 全国主要枢纽站（按地区分组，用于自动搜索最优中转） ==========
HUB_STATIONS_BY_REGION = {
    "华北": ["北京西", "北京南", "北京", "天津西", "天津", "石家庄"],
    "华东": ["上海虹桥", "上海", "上海南", "南京南", "南京", "杭州东", "杭州",
             "济南西", "济南", "合肥南", "合肥", "徐州东", "青岛"],
    "华南": ["广州南", "广州", "广州东", "深圳北", "深圳", "福州", "厦门北", "厦门"],
    "华中": ["武汉", "汉口", "武昌", "郑州东", "郑州", "长沙南", "长沙",
             "洛阳龙门", "宜昌东", "襄阳东", "信阳东"],
    "西南": ["成都东", "成都", "重庆北", "重庆西", "重庆", "昆明南", "昆明",
             "贵阳北", "贵阳", "遵义", "万州北"],
    "西北": ["西安北", "西安", "兰州西", "兰州", "西宁", "银川"],
    "东北": ["沈阳北", "沈阳", "长春西", "长春", "哈尔滨西", "哈尔滨"],
    "其他": ["南昌西", "南昌", "南宁东", "南宁", "太原南", "太原",
             "上饶", "怀化南", "桂林北", "桂林", "柳州", "湛江"],
}
ALL_HUBS = [s for region in HUB_STATIONS_BY_REGION.values() for s in region]


def _min_seat_price(train: dict):
    """取一趟车所有席别中最低的有票价格（普速车硬座也算）。"""
    seats = train.get("seats", {})
    prices = [info["price"] for info in seats.values() if info.get("price") and info["price"] > 0]
    return min(prices) if prices else None


def find_best_route(start_station: str, end_station: str, travel_date: str,
                    start_is_city: bool = False, end_is_city: bool = False,
                    hubs: list = None,
                    progress_callback=None) -> dict:
    """
    自动搜索从 start 到 end 的所有可能路线（直达 + 指定枢纽站中转），
    支持城市模式（自动展开该市所有站）。
    hubs: 指定要考虑的中转站列表，None 表示用全部
    """
    if hubs is None:
        hubs = ALL_HUBS
    start_stations = get_city_stations(start_station) if start_is_city else [start_station]
    end_stations = get_city_stations(end_station) if end_is_city else [end_station]

    all_schemes = []

    # --- 1. 直达方案：每个出发站 × 每个到达站 ---
    for s_start in start_stations:
        for s_end in end_stations:
            direct_raw = query_train_ticket(s_start, s_end, travel_date)
            direct = json.loads(direct_raw)
            time.sleep(0.15)
            if "trains" in direct:
                for t in direct["trains"]:
                    price = _min_seat_price(t)
                    dur_str = t.get("duration", "00:00")
                    parts = dur_str.split(":")
                    try:
                        total_min = int(parts[0]) * 60 + int(parts[1])
                    except Exception:
                        total_min = 0
                    all_schemes.append({
                        "type": "直达",
                        "via": "无",
                        "train_no": t["train_no"],
                        "price": price,
                        "total_minutes": total_min,
                        "seg1": t,
                        "seg2": None,
                    })

    # --- 2. 遍历枢纽站找中转 ---
    total_hubs = len(hubs)
    for idx, mid in enumerate(hubs):
        if mid.strip() in start_stations or mid.strip() in end_stations:
            continue
        if progress_callback:
            progress_callback(idx / total_hubs, f"正在查询 → {mid} ...")

        # 收集所有出发站到中转站的车次
        seg1_all = []
        for s_start in start_stations:
            seg1_raw = query_train_ticket(s_start, mid, travel_date)
            seg1 = json.loads(seg1_raw)
            time.sleep(0.15)
            if "trains" in seg1:
                seg1_all.extend(seg1["trains"])

        if not seg1_all:
            continue

        # 收集中转站到所有到达站的车次
        seg2_all = []
        for s_end in end_stations:
            seg2_raw = query_train_ticket(mid, s_end, travel_date)
            seg2 = json.loads(seg2_raw)
            time.sleep(0.15)
            if "trains" in seg2:
                seg2_all.extend(seg2["trains"])

        if not seg2_all:
            continue

        seg1_merged = json.dumps({"trains": seg1_all}, ensure_ascii=False)
        seg2_merged = json.dumps({"trains": seg2_all}, ensure_ascii=False)
        transfer_raw = calc_transfer_scheme(seg1_merged, seg2_merged)
        transfer = json.loads(transfer_raw)

        candidates = (transfer.get("top5_by_price") or []) + (transfer.get("top5_by_time") or [])
        seen_key = set()
        for s in candidates:
            if not s:
                continue
            k = (s["seg1"]["train_no"], s["seg2"]["train_no"])
            if k in seen_key:
                continue
            seen_key.add(k)
            p1 = _min_seat_price(s["seg1"])
            p2 = _min_seat_price(s["seg2"])
            total_price = (p1 + p2) if (p1 is not None and p2 is not None) else None
            all_schemes.append({
                "type": "中转",
                "via": mid,
                "train_no": f"{s['seg1']['train_no']}+{s['seg2']['train_no']}",
                "price": total_price,
                "total_minutes": s.get("total_minutes"),
                "wait_min": s.get("transfer_wait_min"),
                "seg1": s["seg1"],
                "seg2": s["seg2"],
            })

    # 去重
    seen = set()
    unique = []
    for s in all_schemes:
        k = (s["type"], s["via"], s.get("price"), s["total_minutes"])
        if k not in seen:
            seen.add(k)
            unique.append(s)

    by_price = sorted([s for s in unique if s["price"] is not None],
                      key=lambda x: x["price"])
    by_time = sorted([s for s in unique if s["total_minutes"]],
                     key=lambda x: x["total_minutes"])

    return {
        "total_schemes": len(unique),
        "cheapest": by_price[0] if by_price else None,
        "fastest": by_time[0] if by_time else None,
        "top5_cheap": by_price[:5],
        "top5_fast": by_time[:5],
    }


# ========== 加载知识库 ==========
def load_knowledge() -> str:
    if KNOWLEDGE_FILE.exists():
        return KNOWLEDGE_FILE.read_text(encoding="utf-8")
    return ""


# ========== Streamlit 界面 ==========
st.set_page_config(page_title="AI 火车票查询", page_icon="🚄", layout="wide")
st.title("🚄 眯哥的火车票查询助手")
st.caption("12306 官方数据 · 多轮对话 · 最快/最省路线 · 乘车知识问答 · 免费无限次")
st.info("💡 **怎么用**：填好出发、到达和日期，点「查询」看直达或中转车次；想找最便宜或最快的中转路线，点「🚀 自动搜索最优路线」，记得勾选可考虑的中转站/地区，减少搜索时间。下面聊天框还能直接问问题，比如告诉我明天重庆站到杭州站最省钱的路线。")


if not DEEPSEEK_API_KEY:
    st.error("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")
    st.stop()

knowledge = load_knowledge()


@st.cache_resource
def get_agent():
    return Agent(
        model=DeepSeek(api_key=DEEPSEEK_API_KEY),
        tools=[query_train_ticket, calc_transfer_scheme],
        instructions=f"""你是火车票查询助手，支持多轮对话和乘车知识问答。

## 知识库（用户问退票、改签、儿童票等规则时参考）
{knowledge}

## 工具说明
- query_train_ticket(start, end, date)：查询站到站车次，返回车次号、出发到达站、时间、历时、各席别 remain（余票）和 price（票价元）。
- calc_transfer_scheme(第一段json, 第二段json)：枚举所有中转组合，返回 cheapest（二等座总价最低）、fastest（总时间最短）、top5_by_price、top5_by_time。

## 输出规则
1. 查车次：用 markdown 表格列出所有车次，列：车次、出发站、到达站、出发时间、到达时间、历时、各席别(价格/余票)。表格后标出"🚀 最快"和"💰 最便宜"。
2. 中转查询：先展示 cheapest 和 fastest，再分别列出两段全部车次表。
3. 余票少于5张标"⚠️紧张"。
4. 票价为0或缺失的席别不显示。
5. 用户问规则类问题（退票、改签、儿童票等），直接根据知识库回答，不用调工具。
6. 站名必须是12306官方站名（如"十堰东"不是"十堰"），查不到提示确认。
7. 支持多轮对话：用户说"那换乘呢"、"下午的车呢"等，结合上下文理解意图。
8. 语言简洁。
""",
    )


agent = get_agent()

# ========== 界面：表单查询 + 聊天追问 ==========
if "messages" not in st.session_state:
    st.session_state.messages = []

# 顶部表单
st.subheader("🔍 查询")

# 第一行：出发站/中转站/到达站 + 城市切换
col_start, col_transfer, col_end = st.columns([2, 1, 2])
with col_start:
    start = st.text_input("出发", value="重庆")
    start_mode = st.radio("出发范围", ["车站", "城市"], horizontal=True, key="start_mode")
with col_transfer:
    transfer = st.text_input("中转站（不需要就留空）", value="")
with col_end:
    end = st.text_input("到达", value="杭州")
    end_mode = st.radio("到达范围", ["车站", "城市"], horizontal=True, key="end_mode")

date = st.date_input("出行日期", value=datetime.now().date())

start_is_city = start_mode == "城市"
end_is_city = end_mode == "城市"

# 显示展开的站
if start_is_city:
    s_stations = get_city_stations(start)
    st.caption(f"出发城市 {start} → 共 {len(s_stations)} 个站：{'、'.join(s_stations)}")
if end_is_city:
    e_stations = get_city_stations(end)
    st.caption(f"到达城市 {end} → 共 {len(e_stations)} 个站：{'、'.join(e_stations)}")

col_q1, col_q2 = st.columns(2)
with col_q1:
    if st.button("查询（所有车次信息）", type="primary", use_container_width=True):

        date_str = date.strftime("%Y-%m-%d")
        if transfer.strip():
            prompt = f"帮我查 {start} → {transfer} → {end} 的中转方案，出行日期 {date_str}。先分段查询，再计算中转组合。"
        else:
            prompt = f"帮我查 {start} 到 {end}，{date_str} 的所有直达车次余票。"
        st.session_state.messages.append({"role": "user", "content": prompt})

with col_q2:
    auto_search = st.button("🚀 自动搜索最优路线（找最便宜/最快中转路线）", use_container_width=True)

# 中转站选择（自动搜索用）
with st.expander("🎛️ 选择中转城市（自动搜索时只搜选中的城市，更快更准）", expanded=False):
    if "selected_hubs" not in st.session_state:
        st.session_state.selected_hubs = list(ALL_HUBS)

    col_sel1, col_sel2 = st.columns([1, 3])
    with col_sel1:
        if st.button("✅ 全选", use_container_width=True):
            st.session_state.selected_hubs = list(ALL_HUBS)
            st.rerun()
        if st.button("🗑️ 清空", use_container_width=True):
            st.session_state.selected_hubs = []
            st.rerun()
    with col_sel2:
        options_with_region = []
        for region, cities in HUB_STATIONS_BY_REGION.items():
            for c in cities:
                options_with_region.append(f"[{region}] {c}")

        display_to_city = {f"[{r}] {c}": c for r, cs in HUB_STATIONS_BY_REGION.items() for c in cs}
        city_to_display = {v: k for k, v in display_to_city.items()}

        default_display = [city_to_display[c] for c in st.session_state.selected_hubs if c in city_to_display]

        selected_display = st.multiselect(
            "中转城市（可搜索、可多选）",
            options=options_with_region,
            default=default_display,
            help="只勾选你想考虑的中转城市，不勾远的地方能省很多时间",
        )

        st.session_state.selected_hubs = [display_to_city[d] for d in selected_display]

    st.caption(f"当前选中 {len(st.session_state.selected_hubs)} / {len(ALL_HUBS)} 个城市")

# 自动搜索最优路线（直接计算，不经过 AI）
if auto_search:
    date_str = date.strftime("%Y-%m-%d")
    if not st.session_state.selected_hubs:
        st.warning("请先选择至少一个中转城市")
        st.stop()
    progress_bar = st.progress(0, "正在搜索所有路线...")

    def cb(pct, msg):
        progress_bar.progress(min(pct, 1.0), msg)

    result = find_best_route(start, end, date_str,
                             start_is_city=start_is_city,
                             end_is_city=end_is_city,
                             hubs=st.session_state.selected_hubs,
                             progress_callback=cb)
    progress_bar.progress(1.0, "搜索完成！")

    # 格式化车次为表格行
    def _train_to_table_row(train, highlight_cheapest=False):
        seats = train.get("seats", {})
        priced = [(name, info.get("price"), info.get("remain", ""))
                  for name, info in seats.items() if info.get("price") and info["price"] > 0]
        priced.sort(key=lambda x: x[1])

        cheapest_name = priced[0][0] if priced else None

        row = f"| **{train['train_no']}** | {train['departure_time']} | {train['arrival_time']} | {train.get('duration', '-')} |"
        for seat_name in ["商务座", "一等座", "二等座", "软卧", "硬卧", "硬座", "无座"]:
            info = seats.get(seat_name, {})
            price = info.get("price")
            remain = info.get("remain", "")
            if price and price > 0:
                cell = f"¥{price:.0f}({remain})"
                if highlight_cheapest and seat_name == cheapest_name:
                    cell = f"<font color='red'>**{cell}**</font>"
                row += f" {cell} |"
            else:
                row += " - |"
        return row

    def _table_header():
        return "| 车次 | 出发 | 到达 | 历时 | 商务座 | 一等座 | 二等座 | 软卧 | 硬卧 | 硬座 | 无座 |\n|------|------|------|------|--------|--------|--------|------|------|------|------|"

    lines = [f"### 🚀 自动搜索结果（共 {result['total_schemes']} 条路线）\n"]

    # 最便宜路线详细信息（表格）
    if result["cheapest"]:
        c = result["cheapest"]
        lines.append(f"#### 💰 最便宜路线：**{c['price']:.0f} 元**")
        lines.append(f"- 类型：{c['type']}" + (f"（经 {c['via']}）" if c['via'] != '无' else ""))
        lines.append(f"- 总耗时：{c['total_minutes'] // 60}小时{c['total_minutes'] % 60}分钟")
        if c["seg2"]:
            lines.append(f"- 中转等待：{c.get('wait_min', 0)}分钟")
            lines.append("")
            lines.append(f"**第一段：{c['seg1']['departure_station']} → {c['seg1']['arrival_station']}**")
            lines.append(_table_header())
            lines.append(_train_to_table_row(c['seg1'], highlight_cheapest=True))
            lines.append("")
            lines.append(f"**第二段：{c['seg2']['departure_station']} → {c['seg2']['arrival_station']}**")
            lines.append(_table_header())
            lines.append(_train_to_table_row(c['seg2'], highlight_cheapest=True))
        else:
            lines.append("")
            lines.append(_table_header())
            lines.append(_train_to_table_row(c['seg1'], highlight_cheapest=True))
        lines.append("")
        lines.append("---")
        lines.append("")

    # 最快路线详细信息（表格）
    if result["fastest"]:
        f = result["fastest"]
        lines.append(f"#### ⚡ 最快路线：**{f['total_minutes'] // 60}小时{f['total_minutes'] % 60}分钟**")
        lines.append(f"- 类型：{f['type']}" + (f"（经 {f['via']}）" if f['via'] != '无' else ""))
        lines.append(f"- 最低价：{f['price']:.0f} 元" if f['price'] else "- 最低价：未知")
        if f["seg2"]:
            lines.append(f"- 中转等待：{f.get('wait_min', 0)}分钟")
            lines.append("")
            lines.append(f"**第一段：{f['seg1']['departure_station']} → {f['seg1']['arrival_station']}**")
            lines.append(_table_header())
            lines.append(_train_to_table_row(f['seg1'], highlight_cheapest=True))
            lines.append("")
            lines.append(f"**第二段：{f['seg2']['departure_station']} → {f['seg2']['arrival_station']}**")
            lines.append(_table_header())
            lines.append(_train_to_table_row(f['seg2'], highlight_cheapest=True))
        else:
            lines.append("")
            lines.append(_table_header())
            lines.append(_train_to_table_row(f['seg1'], highlight_cheapest=True))
        lines.append("")
        lines.append("---")
        lines.append("")

    # Top5 表格（简洁版）
    if result["top5_cheap"]:
        lines.append("#### 💰 最便宜 Top 5")
        lines.append("| 排名 | 类型 | 经停 | 车次 | 最低价 | 总耗时 |")
        lines.append("|------|------|------|------|--------|--------|")
        for i, s in enumerate(result["top5_cheap"], 1):
            t_min = s["total_minutes"]
            lines.append(f"| {i} | {s['type']} | {s['via']} | {s['train_no']} | {s['price']:.0f}元 | {t_min//60}h{t_min%60}m |")
        lines.append("")
        lines.append("---")
        lines.append("")

    if result["top5_fast"]:
        lines.append("#### ⚡ 最快 Top 5")
        lines.append("| 排名 | 类型 | 经停 | 车次 | 最低价 | 总耗时 |")
        lines.append("|------|------|------|------|--------|--------|")
        for i, s in enumerate(result["top5_fast"], 1):
            t_min = s["total_minutes"]
            price_str = f"{s['price']:.0f}元" if s['price'] else "未知"
            lines.append(f"| {i} | {s['type']} | {s['via']} | {s['train_no']} | {price_str} | {t_min//60}h{t_min%60}m |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 所有路线：每个路线直接展开成表格
    all_items = []
    if result.get("cheapest"):
        all_items.append(result["cheapest"])
    if result.get("fastest"):
        all_items.append(result["fastest"])
    all_items.extend(result.get("top5_cheap", []))
    all_items.extend(result.get("top5_fast", []))
    seen = set()
    unique_all = []
    for s in all_items:
        k = s["train_no"]
        if k not in seen:
            seen.add(k)
            unique_all.append(s)

    if unique_all:
        lines.append(f"#### 📋 所有路线详情（共 {len(unique_all)} 条）")
        lines.append("")
        for i, s in enumerate(unique_all, 1):
            price_str = f"{s['price']:.0f}元" if s['price'] else "未知"
            t_min = s["total_minutes"]
            lines.append(f"**路线{i}：{s['train_no']}（{price_str}，{t_min//60}h{t_min%60}m）**")
            lines.append("")
            if s["seg2"]:
                lines.append(f"第一段：{s['seg1']['departure_station']} → {s['seg1']['arrival_station']}")
                lines.append(_table_header())
                lines.append(_train_to_table_row(s['seg1']))
                lines.append("")
                lines.append(f"第二段：{s['seg2']['departure_station']} → {s['seg2']['arrival_station']}")
                lines.append(_table_header())
                lines.append(_train_to_table_row(s['seg2']))
            else:
                lines.append(_table_header())
                lines.append(_train_to_table_row(s['seg1']))
            lines.append("")
            lines.append("---")
            lines.append("")

    best_text = "\n".join(lines)
    st.session_state.messages.append({"role": "user", "content": f"自动搜索 {start} → {end} {date_str} 的最优路线"})
    st.session_state.messages.append({"role": "assistant", "content": best_text})

# 显示历史消息
st.divider()
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"], unsafe_allow_html=True)

if not st.session_state.messages:
    st.info("👆 在上面填出发站、到达站和日期，点查询就行。也可以在下方聊天框追问，比如'那下午的车呢'、'退票怎么收费'。")

# 聊天追问
if prompt := st.chat_input("追问或直接输入问题..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt, unsafe_allow_html=True)

# 统一处理 AI 回复（表单查询和聊天共用）
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    with st.chat_message("assistant"):
        with st.spinner("AI 正在查询..."):
            history_text = ""
            for m in st.session_state.messages[:-1]:
                history_text += f"用户：{m['content']}\n" if m["role"] == "user" else f"助手：{m['content']}\n"
            last_q = st.session_state.messages[-1]["content"]
            full_prompt = f"之前的对话：\n{history_text}\n用户现在问：{last_q}" if history_text else last_q
            result = agent.run(full_prompt)
            st.markdown(result.content, unsafe_allow_html=True)
            st.session_state.messages.append({"role": "assistant", "content": result.content})

# 侧边栏清空
if st.session_state.messages:
    if st.sidebar.button("🗑️ 清空对话"):
        st.session_state.messages = []
        st.rerun()
