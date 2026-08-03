${role}

Purpose: maximize plausible review questions before a later full-paper resolver removes false alarms.
For every numbered sentence, decompose every substantive atomic claim. Give at least one precise,
falsifiable question for every atomic claim. Do not cap the number of questions. Do not praise,
resolve, or infer support from unseen context. A sentence may be not-applicable only when it is purely
bibliographic, layout, or connective text; explain why. Where locally applicable, question matched
baselines and compute cost, hyperparameter sensitivity, marginal or negative cases, method mechanics,
error propagation, visual/text consistency, and the scope of evidence. Phrase each question so that it
names the exact comparison, measurement, derivation, clarification, or other evidence that would answer
it; avoid generic requests for "more evidence."

Return exactly ${sentence_count} items, one for each ordinal S1 through S${sentence_count}, with no
missing or extra ordinals. Keep each atomic claim and question concise so the complete coverage fits
in one response; completeness across sentence ordinals takes priority over elaboration.

UNIT TEXT
${numbered_sentences}
