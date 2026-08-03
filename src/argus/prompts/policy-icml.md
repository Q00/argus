ICML-style adjudication policy. These are domain heuristics, deliberately separated from the
structural prompt so they can be revised, versioned, and evaluated independently. Every rule here is
a potential recall/precision trade; the attrition report attributes discards so this file's effect is
measurable.

- Do not turn ordinary motivation words such as "important" or "critical" into an unstated claim that
  the problem outranks every alternative.
- Do not demand a human study for an explicitly qualitative analogy unless the paper presents the
  analogy as measured human agreement.
- Do not reinterpret "outperforms across multiple settings" as "wins every metric in every setting."
- Do not demand causal identification when the paper explicitly labels a relationship as a hypothesis
  or association.
- A missing significance test is final only when the paper claims statistical significance, rather
  than using ordinary descriptive improvement language.
- When the paper provides a partial ablation, describe the exact missing arm or unmatched condition
  rather than claiming that no isolation exists.
- For an inference-efficiency method, a compute concern is solved only by a matched end-to-end
  latency, throughput, memory, or operation comparison against the relevant baseline, not by the
  convention that extra modules generally cost compute.
- Retain a factually located concern when the paper omits directly related baselines, leaves core
  method transitions unreproducible, shows unexplained negative or marginal cases, needs per-setting
  hyperparameter tuning without guidance, conflicts with a Figure or Table, permits cumulative error
  without analysis, or generalizes substantially beyond the evaluated model and task scope.
- Do not require a larger benchmark suite merely because newer models or datasets exist; the stated
  claim scope and supplied evidence must make the gap decision-relevant.
- Discard a generic question about whether a linked repository is complete unless inspected repository
  evidence identifies the concrete missing implementation or artifact needed to reproduce a stated
  result; lack of external confirmation is not evidence of incompleteness.
