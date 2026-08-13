# 🤖 大基吧 · 基金投资日报机器人

每天晚9点自动推送基金涨跌、趋势分析和操作建议到微信。

## 功能

- 📊 每日持仓总览（净值、涨跌、收益）
- 🟢🟡🔴 红绿灯趋势判断（通俗中文解读）
- 🎯 动态止损止盈风控（基于ATR波动率）
- ⚠️ 异动警报（单日涨跌超±3%额外推送）
- 📈 基准指数对比（沪深300、中证电池主题）
- 📥 右侧加仓信号（行业基金深跌止跌转涨确认后，提示递减式加仓）
- 🔧 添加基金只需编辑配置文件

## 技术栈

Python + AKShare + PushPlus + GitHub Actions

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/deliberate-xia/dajiba-fund-bot.git
cd dajiba-fund-bot
pip install -r requirements.txt
```

### 2. 配置 PushPlus Token

创建 `data/preferences.local.json`（已加入 .gitignore）：

```json
{
  "pushplus_user_token": "你的PushPlus Token"
}
```

去 [pushplus.plus](http://pushplus.plus) 注册获取 Token。

### 3. 本地运行

```bash
python src/main.py
```

### 4. GitHub Actions 部署

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 值 |
|-------------|-----|
| `PUSHPLUS_USER_TOKEN` | 你的 PushPlus Token |

系统会在每个交易日 21:17（北京时间）自动运行。

## 添加新基金

编辑 `data/holdings.json`，按格式添加即可。

## 免责声明

本工具仅供学习参考，不构成投资建议。投资有风险，过往业绩不预示未来表现。
