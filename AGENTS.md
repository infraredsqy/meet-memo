# meeting —— 本地会议录制服务

> 本文件是路牌，上限 40 行：一行一处，只说什么在这里、什么时候看。
> **动手之前先读** `docs/rules.md`（测试 / 文档 / 注释三条纪律）与 `docs/design-intent.md`（这项目的思路与理念）。

- `meeting/` —— 服务本体（Python）：`capture/` 采集与归档、`backends/` 四个后端协议与解析、`service/` HTTP 面与运行时、`offline/` 会后离线链、`minutes/` 会后纪要、`summary/` 会中总结、`asr/` 会中识别。改服务看这里
- `web/` —— 前端工程（React + TS + Vite）；构建产物由服务直发，命令与目录见 `web/README.md`
- `tests/` —— 服务端测试：`_*.py` 是共用假件与驱动，`test_*.py` 一主题一份；纪律见 `docs/rules.md`
- `docs/` —— 现状与判据的正本：为什么、需求锚、服务现状、状态与产物、双端契约、界面口径。动设计、改对外形状之前看（索引 `docs/AGENTS.md`）
- `contract/` —— 双端契约的 golden 样本与掩码规则（正本 `contract/README.md`）；服务形状一漂移，`tests/test_contract_fixtures` 就红
- `prompts/` —— 会后纪要的提示词模板
- `README.md` —— 门面：是什么、怎么装起、三条命令、开发与联系
- `pyproject.toml` —— 打包面：依赖分组（`asr` extra 的 CUDA 那格）、console script、pytest 配置与 `slow` 标记
- `.work/` `.tmp/` —— 本机的草稿与临时产物，不进版本库（约定见 `.work/AGENTS.md`）
