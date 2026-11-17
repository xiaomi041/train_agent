# 🚄 AI 火车票查询助手（12306 官方接口版）

基于 **12306 官方余票接口 + DeepSeek 大模型 + Multi-Agent 架构**的智能火车出行规划助手。输入出发站、到达站和日期，即可获取直达/中转全部车次的余票与票价，并自动推荐**总票价最低**、**全程耗时最短**的最优路线；同时支持乘车规则类问题的即时问答。

## ✨ 功能特性

- **🔍 12306 官方余票接口逆向解析** —— 免费、无限制调用，直接解析官方接口返回的车次、到发时刻、历时及 7 类席别（商务座/一等座/二等座/软卧/硬卧/硬座/无座等）余票与票价，无需任何付费第三方 API
- **🗺️ 图搜索最优路线** —— 内置全国主要枢纽站图，遍历所有可行中转组合，枚举「票价最低」「耗时最短」及 Top5 候选方案，支持多级中转
- **🤖 Multi-Agent 架构** —— 主 Agent（DeepSeek + ReAct 多步推理）负责查票规划与工具调度；独立评审 Agent 对候选方案二次挑刺（换乘等待时间、余票充足度、站名一致性、价格对比完整性），保证推荐质量
- **💬 自然语言多轮对话** —— 模糊指令自动拆解（如"帮我安排国庆回家""明天重庆到杭州最省钱的路线"），Function Calling 调度工具，多轮追问自动结合上下文（"那换乘呢""下午的车呢"）
- **📚 RAG 乘车知识库** —— 内置退票/改签/儿童票/学生票/购票时间等乘车规则，本地 Embedding（`bge-small-zh-v1.5`）向量化 + 余弦相似度检索，规则类问题即时准确回答，不凭记忆编造
- **⚡ Streamlit 聊天式界面** —— 表单直查（出发/中转/到达 + 日期，支持车站⇄城市）与自由对话双通道

## 🏗️ 系统架构

```
用户 ──> Streamlit 界面 ──> 主 Agent（DeepSeek + ReAct 推理）
                              │  思考 → 行动 → 观察 → 回答
                              ├── 工具① query_train_ticket   ──> 12306 官方余票接口
                              ├── 工具② calc_transfer_scheme ──> 枢纽站图搜索 · 中转组合枚举
                              ├── 工具③ search_knowledge     ──> 本地向量库（bge-small-zh 余弦检索）
                              ├── 工具④ review_plan          ──> 独立评审 Agent 挑刺
                              └── 工具⑤ get_today            ──> 相对日期解析
```

- **主 Agent**：DeepSeek + Agno，注册 5 个工具，强制 ReAct 多步推理（查直达 → 枚举中转 → 独立评审 → 综合推荐）
- **评审 Agent（Critic）**：独立 DeepSeek 实例，只负责对候选方案挑刺，不参与查票
- **数据层**：12306 官方余票接口（实时数据）、`stations.json`（站点缓存）、`train_knowledge.md`（知识库源，启动时自动向量化）

## 🚀 快速开始

### 环境要求
- Python 3.10+

### 安装

```bash
git clone https://github.com/xiaomi041/train_agent.git
cd train_agent

# 创建虚拟环境（可选但推荐）
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 配置密钥

项目根目录创建 `.env` 文件：

```
# DeepSeek API Key（必填，https://platform.deepseek.com 获取）
DEEPSEEK_API_KEY=你的密钥

# 遇到 SSL 证书校验失败时可设为 1 跳过（仅建议调试时使用）
SKIP_SSL_VERIFY=0
```

### 启动

```bash
streamlit run a.py
```

浏览器访问 `http://localhost:8501`

## 🎯 使用指南

1. **表单直查**：填写出发站/到达站（支持车站或城市，城市会自动展开到下属车站）与出行日期 → 点击「查询（所有车次信息）」查看直达或中转全部车次
2. **自动搜索最优路线**：点击「🚀 自动搜索最优路线」，勾选可接受的中转站/地区后自动搜索，直接给出最便宜/最快方案
3. **自由对话**：直接在聊天框提问，例如：
   - "明天重庆站到杭州站最省钱的路线"
   - "帮我安排国庆回家的行程"
   - "退票手续费怎么算？"（知识库规则问答）
   - "儿童票怎么买？"
4. **多轮追问**：问完一次后继续追问"那换乘呢""下午的车呢"，Agent 会结合上下文继续查询

## 📁 项目结构

```
train_agent/
├── a.py                  # 主程序（Streamlit 界面 / 图搜索 / Multi-Agent / RAG）
├── ticket_api.py         # 12306 访问层（会话、站点映射、余票查询）— 与 UI 解耦，可被复用和离线测试
├── watch.py              # 盯票 Agent（目标约束集 / 轮询决策 / 逐级降级 / 事件留痕）
├── test_watch.py         # 盯票 Agent 的离线测试（33 条断言，不联网不调模型）
├── train_knowledge.md    # 乘车规则知识库（可自行扩充，启动时自动向量化）
├── stations.json         # 12306 站点缓存（首次运行自动生成，已被 git 忽略）
├── logo.jpg              # 页面图标
├── requirements.txt      # 依赖清单
└── .env                  # API 密钥（已被 git 忽略，请勿上传）
```

## 🎯 盯票 Agent（常驻模式）

原来的用法是「问一句、查一次、给个结果」。任务单薄的地方在于：**买票的难点不是查一次，而是盯到有票为止**。
`watch.py` 把它变成一个有状态、会降级、会放弃的常驻任务：

```python
from watch import Goal, Watcher, Store

goal = Goal(origin="十堰", dest="哈尔滨", date="2026-11-20",
            budget=500, max_minutes=840, arrive_before="22:00",
            seat_prefs=["硬卧", "二等座"], interval_sec=300)
store = Store()
store.save_goal(goal)
Watcher(goal, fetcher=real_fetcher, store=store).start()   # 后台轮询
```

三条判据都有代码落点（也是 `test_watch.py` 的断言对象）：

| 判据 | 落点 | 失败会怎样 |
|---|---|---|
| 目标可验证 | `Goal` 是约束集，`check(scheme, goal)` 返回违规项 | 不会被"看起来差不多"的方案骗过去 |
| 多步自主推进 | `decide()` 按本轮观察决定：命中 / 降级 / 等待 / 放弃 | 路径随余票变化，不是固定流水线 |
| 失败自恢复 | `tick()` 区分四类失败 | 暂时性→退避重试；数据缺失→换查法；目标不可达→逐级放宽；内部错误→兜底不编造 |

逐级放宽的顺序（`relax_level` 0→4）：允许中转 → 日期前后浮动 → 席别不限 → 预算 +20% / 到达时段放宽。
每一级都会留下事件记录（`watch.db` 的 `events` 表），界面上可以逐条看它"为什么这么判"。

离线跑测试：

```bash
python test_watch.py      # 33 条断言，秒级完成
python watch.py demo      # 看一遍「无票 → 降级 → 命中」的完整决策链
```


## 🐳 部署（Docker）

```bash
docker build -t train-ai .
docker run -d --name train-ai \
  -p 8502:8501 \
  --env-file .env \
  -v "$(pwd)/cache:/root/.cache" \
  --restart unless-stopped \
  train-ai
```

- 镜像内已安装 `libgomp1`（onnxruntime 依赖），否则 fastembed 导入会失败。
- `/root/.cache` 挂载可缓存 bge-small-zh 模型，避免每次重启重新下载。
- `stations.json` 首次运行自动抓取并落盘，不进镜像。
- 与个人事务项目同时部署时使用根目录的 `docker-compose.yml`；公网访问见 `DEPLOY.md`。

## ⚠️ 免责声明

本项目的数据来自 12306 官方公开的余票查询接口，仅供**学习研究**使用，请勿用于商业用途；出行信息请以 12306 官方渠道为准。

## 📄 License

MIT
