import { defineConfig } from 'vitest/config';

/**
 * vitest 的配置单独放一份，与 `vite.config.ts` 分开。
 *
 * 两份分开的理由是版本线：vitest 2 自带一条 vite 5，而工程用 vite 6，
 * 两者的 `Plugin` 类型对不上号。
 *
 * 那句「混一份会报错」说的是 **`tsc -b`** 那条路径（`--build` 会顺着 project
 * references 把本文件拉进同一次类型检查，于是 vite 的 `Plugin` 撞上 vitest 的
 * `PluginOption`，报一长串类型错）。**当前的 build 链路不走 `tsc -b`** ——
 * `npm run build` 是 `npm run typecheck && vite build`，而 typecheck 是两条
 * `tsc --noEmit -p`，各查各的 project，压根不会把两份配置摆进同一个类型环境。
 * 分开仍然值当：两条线各守各的边界，`tsc -b` 哪天回来也不用先拆文件
 * （根 `.gitignore` 里那行 `*.tsbuildinfo` 是同一件事的另一半）。
 *
 * 测试跑在 node 环境：用例读的是盘上的 golden 样本与 fetch/EventSource 替身，
 * 不需要 DOM。
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
});
