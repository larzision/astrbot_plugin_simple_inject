# 状态注入插件

**版本：1.1.2**

为AstrBot设计的角色状态注入插件。根据对话动态注入时段、情绪、好感度等状态信息，让人设更鲜活。

## 功能

- 自动感知时段（凌晨/清晨/上午/中午/下午/傍晚/晚上）
- 情绪系统，随对话动态变化
- 好感度随互动涨跌
- 淫乱度系统
- 随机结巴效果，每次对话独立触发
- 按QQ号分关系标签（哥哥/朋友/邻居/敌人/普通）
- 对话历史管理，自动修剪防止内存溢出

## 配置项

所有配置项通过管理面板调整。支持**独立开关**，可精确控制注入内容：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `inject_period` | bool | 时段标签 |
| `inject_emotion` | bool | 情绪标签 |
| `inject_affection` | bool | 好感度描述 |
| `inject_lewdness` | bool | 淫乱度描述 |
| `inject_relation` | bool | 关系标签 |
| `inject_time_info` | bool | 消息时间信息 |

其他配置：结巴概率、初始好感/情绪/淫乱度、对话历史条数等。

## 文件结构

```
astrbot_plugin_simple_inject/
├── main.py          # 插件主逻辑
├── _conf_schema.json # 配置项定义
├── metadata.yaml    # 插件元信息
├── logo.png         # 插件图标
└── README.md        # 本文件
```

状态数据持久化存储于 `data/plugin_data/simple_inject/`

## 依赖

无额外依赖，AstrBot 原生可运行。

---

*—— by miko，一个只会打galgame的阴暗宅女*
