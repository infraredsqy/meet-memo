# meeting 项目指令

> 本文件（AGENTS.md / CLAUDE.md）不得超过 100 行。

## 测试档

> 测试档：full

节点验收用全量档：服务端走 `python -m pytest -q`（含慢档），前端走 `cd web && npm test` 与
`npm run typecheck`，`npm run build` 收尾。选型依据见 README 第 6 节的快/慢/全三档纪律。
