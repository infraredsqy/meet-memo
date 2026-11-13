"""`tests/test_contract_fixtures/` —— `contract/fixtures/` 的采集与漂移比对（设计）。

两个模式（开关注册在 `tests/conftest.py` 的 `pytest_addoption`）：

  - **默认**：重新走一遍真实服务路径采集样本，与 `contract/fixtures/` 逐文件比对，
    缺文件或内容不一致即红 —— 服务侧的形状漂移在这一步被拦下；
  - **`--regen-contract`**：同样的采集，改写 fixtures。掩码（`tests/_contract.py` 的
    `normalize()`）把墙钟、路径、端口、电平这些每跑必变的量收成自描述占位符，
    于是连跑两次 `--regen-contract` 落盘的内容逐字相同；`revision` 保留真实值 ——
    它由固定的迁移与提交次数决定，同样确定。

样本出处（更细的出处表在 `contract/README.md`）：

  - **进程内 TestClient**（真实 lifespan + startup 钩子 + 动作端点）：idle / recording /
    alert / ended·done / ended·draining 的快照、四个心跳样本（idle / recording /
    还没有第一帧 / 帧空洞留痕）、成功动作、七类失败动作、
    resync 帧（`hub.py` 的真实广播路径：真订阅者队列灌满 512 后的下一次广播走 `resync()`）；
  - **真服务子进程**：SSE 帧样本（dev 采集替身 + `module:Class` 假转写后端，见
    `tests/_contract.write_transcriber_stub()`）。

结构断言（在掩码前的原始载荷上做）：快照顶层九键、两路次序、录音态包络恰 15 根、
`bar == envelope[-1]`、levels 帧 `ts` 恒 null、resync 帧顶层额外 `resync` / `dropped` 两键。
"""
