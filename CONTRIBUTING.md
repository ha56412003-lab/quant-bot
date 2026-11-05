# 贡献指南

## 项目定位

这是一个**个人量化交易系统**，主要服务于单账户、单机部署场景。

它不是一个通用框架，不追求支持所有交易所、所有策略类型。所以：

- **欢迎**：bug 报告、风控逻辑的改进建议、指标实现的正确性纠错
- **谨慎接受**：新增交易所适配、大规模架构重构
- **不接受**：把它改造成多租户 SaaS、加入主观判断类策略

## 开发环境

### 本地跑起来

```bash
# 1. 克隆
git clone <your-repo-url>
cd quant-bot

# 2. 创建虚拟环境（不要用系统 Python）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

# 3. 装依赖
pip install -r requirements.txt

# 4. 配置密钥
copy .env.example .env          # Windows
# cp .env.example .env          # Linux/Mac
# 然后编辑 .env 填入你自己的 key

# 5. 启动后端
python -m backend.main

# 6. 前端（另开一个终端）
cd frontend-v2
npm install
npm run dev
```

后端默认 `http://127.0.0.1:8899`，前端开发服务器默认 `http://127.0.0.1:5173`。

### 前端构建

```bash
cd frontend-v2
npm run build          # 产物在 dist/
```

后端会自动读取 `frontend-v2/dist`，构建完刷新浏览器即可。

## 代码风格

| 项 | 约定 |
|---|---|
| Python | 4 空格缩进，单行 ≤120 字符，类型注解尽量加 |
| JS/JSX | 2 空格缩进，函数组件 + hooks，不用 class 组件 |
| 命名 | Python 用 `snake_case`，JS 用 `camelCase`，组件用 `PascalCase` |
| 注释 | 中文为主（本项目面向中文用户） |
| 格式化 | 见 `.editorconfig` |

## 提交规范

```
<类型>: <简短描述>

<可选的详细说明>
```

类型：

| 类型 | 含义 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 重构（不改行为） |
| `docs` | 文档 |
| `perf` | 性能优化 |
| `test` | 测试 |
| `chore` | 杂项（依赖、构建） |

示例：

```
fix: 修正 mark_price_of 在拿不到价格时静默返回 0

之前 POLUSDT 空单因为拿不到行情，浮盈被算成 +573.5U，实际是 -17.8U。
现在改为抛异常 + 调用方兜底，并在 daily_review_data.py 里加了价格预取缓存。
```

## 测试

### 改交易逻辑前必读

**这个系统会动钱（哪怕现在是模拟盘）。** 任何改动交易逻辑的 PR 必须做到：

1. **说明改动会影响哪条链路**（开仓 / 平仓 / 止损 / 风控）
2. **给出前后对比的实测数据**（不是「我觉得应该对」）
3. **在 paper 模式下跑过至少一个完整交易周期**

### 跑测试

```bash
# 后端单测（如果有）
python -m pytest backend/backtest/tests/ -v

# 回测
python -m backend.backtest --help
```

### 前端改动

**build 通过 ≠ 页面能跑。** 改完必须在浏览器里逐页点一遍：

```
/                总览
/?page=strategies 策略库
/?page=factors    因子库
/?page=trades     交易日志
/?page=risk       风控
/?page=pool       监控池
```

## 报告 bug

请提供：

1. **现象**：发生了什么，期望是什么
2. **复现步骤**：怎么操作能重现
3. **环境**：操作系统、Python 版本、是否跑过 `pip install -r requirements.txt`
4. **日志**：`logs/` 目录下相关片段
5. **数据**：如果和某笔交易有关，给出币种、开仓时间、仓位 ID

**不要贴 `.env` 的内容。** 涉及密钥的地方一律打码。

## 风控相关改动的额外要求

任何改动 `backend/risk.py`、`backend/executor.py`、`strategies.yaml` 里风控参数的 PR，必须在描述里写清：

```
- 改动前：并发上限 6、单边上限 6、每单风险 0.5%
- 改动后：并发上限 8、单边上限 6、每单风险 0.5%
- 影响：单边 6 × 0.5% = 3.0% 正好等于熔断线 3%，无缓冲
- 验证：跑了 N 笔交易，最大同时持仓 8，未触发熔断
```

参数不是拍脑袋调的，每一个都要有理由。
