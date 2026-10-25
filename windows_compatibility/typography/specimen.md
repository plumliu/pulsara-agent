# Pulsara 排版样张 Typography

## 中英文正文与层级 / Reading

这是中文正文，保留宋体风格的笔形与标点。Pulsara keeps the familiar reading rhythm. 0123456789，16:08，19.5%。

**中文粗体与 Bold Latin**；*中文斜体与 Italic Latin*；***粗斜体 Bold italic***。

- 无序列表：Chinese and English，保留原有节奏。
- 第二项包含行内代码 `Path("中文目录")`。

1. 有序列表：编号与文字对齐。

> 引文引用：Keep the baseline, keep the details.

```python
from pathlib import Path
workspace = Path("中文 工作区")
print("Pulsara", 123.45)
# 对齐 / spacing:  0O 1Il  [] {} =>
```

特殊符号：— – “ ” ‘ ’ ￥ € $ ≤ ≥ → ← ∑ ∞ α β γ

$$
\sum_{i=1}^{n}i=\frac{n(n+1)}2\quad \int_0^1 x^2\,dx=\frac13\quad \mathbb{R}\quad\mathcal{L}\quad\text{中文面积}
$$

## 纯字形矩阵的文本

- Latin: Pulsara reads quietly. The quick brown fox: ffi, fi, AV, To.
- 中文：中文正文与界面：苹方、宋体，保持原有排版。
- 数字：0123456789 19.5% 2026-10-26 16:08
- 混排：Pulsara 中文 mixed 0123456789 — “quotes” ￥ $ ≤ ≥ → ∑

以上四段分别用UI、Body、Mono，以及normal/bold/italic/bold-italic测量。字形矩阵、阅读结构和真实Pulsara界面是不同层级证据；实际复制/编辑交互由原React组件测试覆盖，不把静态样张当完整GUI会话。
