"""
城市坐标与中转枢纽筛选。

为什么需要
----------
原来 `watch.real_fetcher` 取 `hubs[:3]` —— 也就是**列表里排在前面的三个城市**，
与乘客从哪到哪毫无关系。北京→上海会拿北京/天津/石家庄当中转站，乌鲁木齐→福州
可能拿哈尔滨当中转站，等于把"智能中转"退化成了"盲取前三个"。

做法：绕行比（detour ratio）
--------------------------
对每个候选枢纽 h 计算

    detour(h) = dist(起点, h) + dist(h, 终点) - dist(起点, 终点)

直线上（几乎不绕路）的枢纽 detour ≈ 0；完全反方向的枢纽 detour ≈ 2×全程。
只保留 `detour / 全程 ≤ MAX_DETOUR_RATIO` 的枢纽，再按 detour 升序取前 k 个。

坐标是**城市级别、近似值**（精确到 0.01°，约 1km 量级）——这里只用它做
"大致在同一方向"的排序，不需要测地精度，所以用球面近似即可。

代价：这只解决"选哪个枢纽合理"，不能保证该枢纽当天真的有联程票；
真正的可行性仍由后续查询与 `check()` 判定。
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

# 城市 → (纬度, 经度)。覆盖 HUB_STATIONS_BY_REGION 里出现的全部城市。
CITY_COORDS: dict[str, tuple[float, float]] = {
    "北京": (39.90, 116.41),
    "天津": (39.13, 117.20),
    "石家庄": (38.04, 114.51),
    "上海": (31.23, 121.47),
    "南京": (32.06, 118.80),
    "杭州": (30.27, 120.15),
    "济南": (36.65, 117.12),
    "合肥": (31.82, 117.23),
    "徐州": (34.26, 117.18),
    "青岛": (36.07, 120.38),
    "广州": (23.13, 113.26),
    "深圳": (22.54, 114.06),
    "福州": (26.07, 119.30),
    "厦门": (24.48, 118.09),
    "武汉": (30.59, 114.30),
    "郑州": (34.75, 113.62),
    "长沙": (28.23, 112.94),
    "洛阳": (34.62, 112.45),
    "宜昌": (30.69, 111.29),
    "襄阳": (32.04, 112.14),
    "信阳": (32.13, 114.09),
    "成都": (30.57, 104.07),
    "重庆": (29.56, 106.55),
    "昆明": (25.04, 102.72),
    "贵阳": (26.65, 106.63),
    "遵义": (27.73, 106.93),
    "万州": (30.81, 108.41),
    "西安": (34.34, 108.94),
    "兰州": (36.06, 103.83),
    "西宁": (36.62, 101.78),
    "银川": (38.49, 106.23),
    "沈阳": (41.80, 123.43),
    "长春": (43.82, 125.32),
    "哈尔滨": (45.80, 126.53),
    "南昌": (28.68, 115.86),
    "南宁": (22.82, 108.32),
    "太原": (37.87, 112.55),
    "上饶": (28.45, 117.94),
    "怀化": (27.55, 109.98),
    "桂林": (25.27, 110.29),
    "柳州": (24.31, 109.41),
    "湛江": (21.27, 110.36),
}

# 站名后缀：只用于把「上海虹桥」这类站名归到城市
_STATION_SUFFIXES = ("虹桥", "龙门", "东", "西", "南", "北")
# 站名与城市不同名的情况（站名不带城市前缀时按城市处理）
_STATION_ALIASES = {
    "汉口": "武汉",
    "武昌": "武汉",
}

MAX_DETOUR_RATIO = 0.6
# 中转站离端点太近没有换乘价值：北京→上海在天津换乘等于刚上车就下车。
MIN_PROGRESS = 0.12     # 沿线路方向至少要走出全程的 12%
MAX_PROGRESS = 0.92     # 也不能快到终点了才换乘
# 垂直于线路的偏移：超过全程的 25% 说明这个枢纽根本不在这个方向上
# 中段偏好的权重：只作为次序修正，不能盖过「绕不绕路」这个主信号
MIDPOINT_WEIGHT = 0.6
# （单看绕行比会漏掉这种：京广线全程近 1900km，济南只多绕 20% 却完全在反方向）
MAX_LATERAL_RATIO = 0.25


def city_of(station: str) -> str:
    """把「广州南」「上海虹桥」这类站名归并到城市名；认不出就原样返回。"""
    name = (station or "").strip()
    if not name:
        return ""
    if name in _STATION_ALIASES:
        return _STATION_ALIASES[name]
    if name in CITY_COORDS:
        return name
    for suffix in _STATION_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            candidate = name[: -len(suffix)]
            if candidate in CITY_COORDS:
                return candidate
    return name


def coord(station: str) -> Optional[tuple[float, float]]:
    return CITY_COORDS.get(city_of(station))


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面距离（公里）。"""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def _legs(origin: str, dest: str, hub: str) -> Optional[tuple[float, float, float]]:
    """返回 (起点→枢纽, 枢纽→终点, 起点→终点) 三个距离（公里）。"""
    o, d, h = coord(origin), coord(dest), coord(hub)
    if not (o and d and h):
        return None
    return _haversine(o, h), _haversine(h, d), _haversine(o, d)


def detour_ratio(origin: str, dest: str, hub: str) -> Optional[float]:
    """绕行比：0 = 在直线上，2 = 完全反方向。坐标缺失返回 None。"""
    legs = _legs(origin, dest, hub)
    if not legs:
        return None
    leg_a, leg_b, base = legs
    if base < 1.0:  # 同城或极近，绕行比无意义
        return None
    return (leg_a + leg_b - base) / base


def _xy(ref: tuple[float, float], point: tuple[float, float]) -> tuple[float, float]:
    """以 ref 为原点的等距投影（公里）。小范围内足够精确，用于判断方向。"""
    lat0 = math.radians(ref[0])
    return (
        (point[1] - ref[1]) * 111.32 * math.cos(lat0),
        (point[0] - ref[0]) * 110.57,
    )


def projection(origin: str, dest: str, hub: str) -> Optional[tuple[float, float]]:
    """返回 (沿线路进度 t, 横向偏移比)。

    t=0 在起点、t=1 在终点；横向偏移比 = 垂直于线路的距离 / 全程距离。
    比绕行比更能识别"方向不对"的枢纽。
    """
    o, d, h = coord(origin), coord(dest), coord(hub)
    if not (o and d and h):
        return None
    dx, dy = _xy(o, d)
    base = math.hypot(dx, dy)
    if base < 1.0:
        return None
    hx, hy = _xy(o, h)
    t = (hx * dx + hy * dy) / (base * base)
    cross = abs(hx * dy - hy * dx)
    # 垂直距离 = cross / base，再除以全程得到"横向偏移比"
    return t, cross / (base * base)


def pick_hubs(
    origin: str,
    dest: str,
    hubs: Iterable[str],
    k: int = 3,
    max_ratio: float = MAX_DETOUR_RATIO,
    min_progress: float = MIN_PROGRESS,
    max_progress: float = MAX_PROGRESS,
    max_lateral: float = MAX_LATERAL_RATIO,
) -> list[str]:
    """按"是否顺路"挑中转枢纽。

    过滤条件（逐条都要满足）：
    - 不与起点/终点同城（同城中转没有意义）
    - 沿线路进度 `t ∈ [min_progress, max_progress]`（太近或太远都不算中转）
    - 横向偏移比 ≤ `max_lateral`（方向不对的直接排除）
    - 绕行比 ≤ `max_ratio`（兜底，防止"方向对但绕很远"）
    - 同一城市只保留一个车站（天津/天津西 不重复占名额）

    排序：`2×|进度−0.5| + 绕行比` 升序 —— 也就是**优先取线路中段、且几乎不绕路**的枢纽。
    只看"离起点最近"是错的：北京→广州在石家庄换乘（全程 15%）相当于没换；
    在中段的武汉/郑州换乘才能把两段都压到 5 小时以内。

    坐标缺失时回退到原始顺序的前 k 个，不因为查不到坐标就一个都不选。
    """
    hubs = list(hubs)
    o_city, d_city = city_of(origin), city_of(dest)
    scored: list[tuple[float, str]] = []

    for hub in hubs:
        h_city = city_of(hub)
        if h_city in (o_city, d_city):
            continue
        proj = projection(origin, dest, hub)
        if not proj:
            continue
        t, lateral = proj
        if not (min_progress <= t <= max_progress):
            continue
        if lateral > max_lateral:
            continue
        ratio = detour_ratio(origin, dest, hub)
        if ratio is None or ratio > max_ratio:
            continue
        scored.append((ratio + MIDPOINT_WEIGHT * abs(t - 0.5), hub))

    scored.sort(key=lambda x: (x[0], x[1]))

    picked: list[str] = []
    seen_cities: set[str] = set()
    for _, hub in scored:
        c = city_of(hub)
        if c in seen_cities:
            continue
        seen_cities.add(c)
        picked.append(hub)
        if len(picked) >= k:
            break
    if picked:
        return picked

    # 一个顺路的都没有（例如起终点都在西部、候选枢纽全在东部）：
    # 与其返回空列表让调用方"没有中转可查"，不如退回原始顺序的前 k 个。
    fallback: list[str] = []
    seen = set()
    for h in hubs:
        c = city_of(h)
        if c in (o_city, d_city) or c in seen:
            continue
        seen.add(c)
        fallback.append(h)
        if len(fallback) >= k:
            break
    return fallback
