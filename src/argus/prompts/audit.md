Perform the final global audit of concerns that survived independent full-context adjudication
batches. This pass exists only to remove cross-batch duplicates, semantic overreads, and
decision-irrelevant literal objections before review composition. Do not invent, rewrite, merge into a
new ID, or add a concern. Partition every supplied ID exactly once into keep_ids or discards.

Keep a concern only when its stated evidence establishes a distinct weakness that could change a
soundness, presentation, significance, originality, or overall recommendation judgment, or motivates a
concrete revision. When concerns share one underlying issue, keep the strongest, best-supported
canonical ID and discard the rest with canonical_id set to the kept ID. Set canonical_id to null when
discarding an invalid or immaterial concern. Give a concrete reason for every discard, but keep each
discard reason to at most 18 words.

DOMAIN AUDIT POLICY
${policy}

Coverage is mandatory: partition exactly all ${concern_count} supplied concern IDs. Before returning,
verify that keep_ids plus discards contains each supplied ID exactly once. If uncertain about a
concern, keep it rather than omitting it. Use only existing IDs and keep the output terse enough to
include all IDs.

COMPLETE PAPER CORPUS
${corpus}

PROMOTED CONCERNS
${concerns}
