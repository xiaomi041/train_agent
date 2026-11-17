"""geo.py 的离线测试：枢纽筛选靠的是"顺路"，不是列表顺序。"""
from __future__ import annotations

import sys

import geo

_passed = 0
_failed = 0


def ok(name: str, cond: bool, extra=None) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {extra if extra is not None else ''}")


HUBS = [
    "北京西", "北京南", "北京", "天津西", "天津", "石家庄",
    "上海虹桥", "上海", "上海南", "南京南", "南京", "杭州东", "杭州",
    "济南西", "济南", "合肥南", "合肥", "徐州东", "青岛",
    "广州南", "深圳北", "武汉", "郑州东", "长沙南", "西安北",
    "沈阳北", "南昌西", "太原南",
]


def main() -> int:
    print("== 1. 站名归并到城市 ==")
    ok("广州南→广州", geo.city_of("广州南") == "广州")
    ok("上海虹桥→上海", geo.city_of("上海虹桥") == "上海")
    ok("汉口→武汉（别名）", geo.city_of("汉口") == "武汉")
    ok("洛阳龙门→洛阳", geo.city_of("洛阳龙门") == "洛阳")
    ok("城市名原样返回", geo.city_of("厦门") == "厦门")
    ok("未知站名不炸", geo.city_of("某个不存在的站") == "某个不存在的站")

    print("== 2. 顺路的进、绕路的不进 ==")
    picked = geo.pick_hubs("北京", "上海", HUBS, k=3)
    ok("北京→上海 挑到 3 个", len(picked) == 3, picked)
    ok("不选同城的北京/上海", not any(geo.city_of(h) in ("北京", "上海") for h in picked), picked)
    ok("不选明显绕路的广州/深圳",
       not any(geo.city_of(h) in ("广州", "深圳", "长沙", "南昌", "沈阳") for h in picked), picked)
    # 济南、青岛、徐州在直线上；南京略绕但仍在阈值内
    ok("挑到的是干线上的枢纽", all(geo.city_of(h) in ("济南", "青岛", "徐州", "南京", "石家庄") for h in picked), picked)
    ok("同一城市不会重复占名额", len({geo.city_of(h) for h in picked}) == len(picked), picked)

    print("== 3. 离端点太近的枢纽被排除 ==")
    # 天津离北京只有 ~110km（全程 1067km 的 10%），刚上车就下车，没有换乘价值
    ok("天津因离起点太近被排除", "天津" not in picked and "天津西" not in picked, picked)
    ratio_tj = geo.detour_ratio("北京", "上海", "天津")
    ok("天津的绕行比确实很小（说明不是被绕行比刷掉的）", ratio_tj is not None and ratio_tj < 0.05, ratio_tj)

    print("== 4. 方向相反时选择随之改变 ==")
    south = geo.pick_hubs("北京", "广州", HUBS, k=3)
    ok("北京→广州 选武汉/长沙方向",
       any(geo.city_of(h) in ("武汉", "长沙", "郑州") for h in south), south)
    ok("北京→广州 不会选青岛/济南",
       not any(geo.city_of(h) in ("青岛", "济南") for h in south), south)

    print("== 5. 坐标缺失时回退原始顺序，而不是全部丢弃 ==")
    fallback = geo.pick_hubs("乌鲁木齐", "拉萨", HUBS, k=2)
    ok("未知城市仍给出候选（回退）", len(fallback) == 2, fallback)

    print("== 6. 起终点同城时不返回空 ==")
    same = geo.pick_hubs("北京", "北京南", HUBS, k=2)
    ok("同城也能给出候选", len(same) >= 1, same)

    print("\n" + "=" * 52)
    print(f"结果：{_passed} 通过，{_failed} 失败")
    print("=" * 52)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
