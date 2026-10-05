# myStock2 文档导航

> 更新：2026-10-05。阶段：实施中（方案 v1.0 已定稿）；M1–M9 合成数据完成，真实数据首跑与历史回填已完成，例行更新已上线。

## 现行文档

| 文档 | 用途 |
| --- | --- |
| [实施方案 v1.0/v1.1](plans/mystock-v2-implementation-plan_claude_20261005.md) | **现行**：里程碑、架构、数据模型、协议、测试、待决事项（v1.0 定稿，§14 为 M0a 发现） |
| [评审记录与处置](plans/plan-review-log_claude_20261005.md) | 逐条处置；含对 gpt-6.1 评审的回应 |
| gpt-6.1 评审原文：[第一轮](plans/plan-review_gpt_20261005.md) · [第二轮](plans/plan-review_gpt_r2_20261005.md) · [第三轮](plans/plan-review_gpt_r3_20261005.md) · [第四轮](plans/plan-review_gpt_r4_20261005.md) · [第五轮](plans/plan-review_gpt_r5_20261005.md) · [第六轮](plans/plan-review_gpt_r6_20261005.md) · [第七轮](plans/plan-review_gpt_r7_20261005.md) · [评审提示](plans/plan-review-prompt_claude_20261005.md) | 独立评审 |
| grok-4.7 评审原文：[首轮](plans/plan-review_grok_20261005.md) · [确认轮](plans/plan-review_grok_r2_20261005.md) | 独立评审（经 cursor-agent） |
| [PROJECT.md](PROJECT.md) | 立项书（一页） |
| [OPEN_ITEMS.md](OPEN_ITEMS.md) | 跨轮次唯一待办与待决事项 |
| [日常数据更新](guides/daily-update_claude_20261005.md) | **一条命令更新数据；launchd 例行任务；重建** |
| [真实数据阶段改动记录](records/real-data-phase-changelog_claude_20261005.md) | 真实首跑以来的决定、提交、偏差、数据现状（回溯用） |
| [真实首跑回执](records/first-real-run_claude_20261005.md) | OpenD 只读采集、V1 导入、重建与核对 |
| [真实数据启动指南](guides/real-data-startup_claude_20261005.md) · [切换运行手册](guides/cutover-runbook_claude_20261005.md) | 从合成到真实前向观察；V1→V2 端口切换 |
| [COLLABORATION.md](COLLABORATION.md) | 协作与多模型评审约定 |

## 输入材料（非现行规格）

| 文档 | 说明 |
| --- | --- |
| [V2项目 idea.md](V2项目%20idea.md) | 项目负责人的原始想法 |
| [prd/mystock-project-v2_claude_20261003.md](prd/mystock-project-v2_claude_20261003.md) | Claude 版 V2 项目书 v2.2（独立版；[HTML](prd/mystock-project-v2_claude_20261003.html)） |
| [prd/mystock-project-v2_codex_20261003.md](prd/mystock-project-v2_codex_20261003.md) | Codex 版 V2 项目书 v2.0（[HTML](prd/mystock-project-v2_codex_20261003.html)） |

两份项目书均以「渐进重构 V1」为前提撰写；V2 实为新仓库，其取舍与合并见实施方案 §2。

## 目录

`plans/` 方案与评审记录 · `prd/` 需求 · `adr/` 架构决策 · `protocols/` 评价协议与冻结配置 · `records/` 工单与回执 · `research/` 调研 · `guides/` 指南。命名规则见 [COLLABORATION.md](COLLABORATION.md) §2。
