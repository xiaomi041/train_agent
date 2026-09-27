import json, sys
sys.path.insert(0, '.')
from a import query_train_ticket, calc_transfer_scheme, get_city_stations, _min_seat_price

# 测试城市展开
print("遵义站:", get_city_stations("遵义"))
print("十堰站:", get_city_stations("十堰"))

# 测试直达
print("\n--- 直达测试 ---")
for s in ["遵义", "遵义西"]:
    for e in ["十堰", "十堰东"]:
        raw = query_train_ticket(s, e, "2026-09-28")
        d = json.loads(raw)
        n = len(d.get("trains", []))
        print(f"{s}→{e}: {n}趟")

# 测试重庆西中转
print("\n--- 重庆西中转测试 ---")
seg1_all = []
for s in ["遵义", "遵义西"]:
    raw = query_train_ticket(s, "重庆西", "2026-09-28")
    d = json.loads(raw)
    trains = d.get("trains", [])
    print(f"{s}→重庆西: {len(trains)}趟")
    seg1_all.extend(trains)

seg2_all = []
for e in ["十堰", "十堰东"]:
    raw = query_train_ticket("重庆西", e, "2026-09-28")
    d = json.loads(raw)
    trains = d.get("trains", [])
    print(f"重庆西→{e}: {len(trains)}趟")
    seg2_all.extend(trains)

print(f"\nseg1合并: {len(seg1_all)}趟")
print(f"seg2合并: {len(seg2_all)}趟")

if seg1_all and seg2_all:
    s1 = json.dumps({"trains": seg1_all}, ensure_ascii=False)
    s2 = json.dumps({"trains": seg2_all}, ensure_ascii=False)
    result = json.loads(calc_transfer_scheme(s1, s2))
    print(f"中转组合数: {result['total_count']}")
    if result['cheapest']:
        c = result['cheapest']
        p1 = _min_seat_price(c['seg1'])
        p2 = _min_seat_price(c['seg2'])
        print(f"最便宜: {c['seg1']['train_no']}+{c['seg2']['train_no']} 价格={p1+p2}")
    for s in result.get('top5_by_price', []):
        p1 = _min_seat_price(s['seg1'])
        p2 = _min_seat_price(s['seg2'])
        print(f"  {s['seg1']['train_no']}+{s['seg2']['train_no']} 价格={p1+p2}")
