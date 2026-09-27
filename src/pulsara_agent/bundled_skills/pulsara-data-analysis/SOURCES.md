# Pulsara Data Analysis — source notes

Reference notes for maintainers; not required to use this Skill. Guidance and the example are written for Pulsara. No upstream Skill scripts or chart library bundles are vendored. Upstream resources retain their own licenses.

| Reference | Version | Areas considered |
| --- | --- | --- |
| OpenAI Codex Data Analytics (installed official package; Proprietary) | `1.0.11` | 业务问题、指标定义、来源与结论核验、按需诊断与交付分工 |
| [Anthropic Data](https://github.com/anthropics/knowledge-work-plugins/tree/da38ec1ee89d41e5380e652a97382695003396e7/data) | `da38ec1ee89d` | 通用分析流程、探索、SQL、统计、可视化与结果验证 |
| [ByteDance DeerFlow Data Analysis](https://github.com/bytedance/deer-flow/tree/81c5c833843ac165e634899f7e7671e98e305bbe/skills/public/data-analysis) | `81c5c833843a` | DuckDB 查询本地数据、结构检查、聚合与结果导出 |
| [Cline Data Analyst](https://github.com/cline/skills/tree/26378461e978f2b4e2e6d67b57121b86b2a79ba5) | `26378461e978` | 明确指标、群体、时间和粒度；数据字典与查询规则分层 |
| [Hermes Jupyter Notebook](https://github.com/NousResearch/hermes-agent/tree/0d9329bd95f0466472f24f2e6e186718ab9e1d40/optional-skills/data-science/jupyter-notebook) | `0d9329bd95f0` | 有状态探索、Notebook 单元操作、必要时重新执行验证 |
| [Astronomer Analyzing Data](https://github.com/astronomer/agents/tree/cbe1141f547bcf0506babb9778a7696bf15eff66/skills/analyzing-data) | `cbe1141f547b` | 仓库查询、复用领域知识、SQL 与 Python 连续探索 |
| [K-Dense Scientific Analysis](https://github.com/K-Dense-AI/scientific-agent-skills/tree/49c6e97775eaa18ba791bebe23162a70ae601c18) | `49c6e97775ea` | 实验单位、缺失机制、探索与验证区别、检验选择、效应与不确定性 |
| [Claude Office Skills Data Analysis](https://github.com/claude-office-skills/skills/tree/9c4c7d5cd2813a8936bf2c9fdb174ea883b85a11/data-analysis) | `9c4c7d5cd281` | 简短办公分析模板，适合比较轻量提示词的覆盖范围 |
| [Jeffallan Pandas Pro](https://github.com/Jeffallan/claude-skills/tree/882ef55e377dbf9a4dbe496bb41ac6ccd0e555cf/skills/pandas-pro) | `882ef55e377d` | DataFrame 处理、类型、连接关系校验与向量化实现 |
| [Lingzhi Research Data Analysis](https://github.com/lingzhi227/agent-research-skills/tree/9e6c085d65e313e475e921fdfe795ac11eb7589e/skills/data-analysis) | `9e6c085d65e3` | 实验结果检查、表内与表间一致性、统计输出格式 |
| [Mindrally Data Analysis Jupyter](https://github.com/Mindrally/skills/tree/97184105b5daa3a6860a2aeb8e7e7fd1c42da40a/data-analysis-jupyter) | `97184105b5da` | Notebook 可复现、分步说明、类型与时区、适量绘图实践 |
| [Alireza Statistical Analysis and Data Quality](https://github.com/alirezarezvani/claude-skills/tree/19392f7a08264ed00486a251f5b2098321771f94) | `19392f7a0826` | 统计决策、效应大小、样本量与数据质量审查的任务分流 |

Library and product contracts consulted:

- [uv script dependencies](https://docs.astral.sh/uv/guides/scripts/) and [environments](https://docs.astral.sh/uv/pip/environments/).
- [pandas joins](https://pandas.pydata.org/docs/reference/api/pandas.merge.html).
- [Plotly self-contained HTML](https://plotly.com/python/interactive-html-export/).
- scikit-learn [PCA](https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.PCA.html) and [silhouette analysis](https://scikit-learn.org/stable/auto_examples/cluster/plot_kmeans_silhouette_analysis.html): feature scaling, explained variance, and interpreting clustering diagnostics in the optional multi-view example.
- Pulsara `visualization_render` tool descriptor, source parser, and `PULSARA_VISUALIZATION_SUBSCRIPTION_IMPLEMENTATION_SPEC.zh.md`: workspace-root paths, inline resources, publication timing, and optional screenshot review.

Lingzhi source was read for workflow comparison; no repository license was identified in the collected version and no code was copied. Professional/statistical references inform optional methods; they do not mandate a full audit or specialized workflow for every task.
