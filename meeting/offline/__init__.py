"""会后离线转写与说话人聚类。

**本包不 import 任何重依赖**：`torch` / `transformers` / `av` / `webrtcvad` 一律在函数体内
惰性 import。包级 import 只该拉进标准库与包内模块，否则「没装模型依赖的机器上 import 本包」
这件事会直接失败。
"""
