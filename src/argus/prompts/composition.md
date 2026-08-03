Write the structured fields of a one-shot review using the complete paper corpus and the already
adjudicated final concerns below. The summary must faithfully state the paper's problem, method, and
principal evidence without adding claims. Strengths must be specific and evidenced by the corpus;
attach section, page, Figure, or Table locators to empirical claims whenever the corpus supplies them.
Do not invent weaknesses or request a rebuttal. Assess strengths and weaknesses across soundness,
presentation, significance, and originality. concern_order must contain every supplied concern ID
exactly once, ordered by materiality and decision relevance. key_question_ids may contain at most five
distinct IDs drawn from those final concerns; this system does not run a rebuttal loop. Scores use the
scales: the four dimensions 1-4, overall recommendation 1-6, and confidence 1-5.
If no final concern remains, do not smuggle an unadjudicated paper weakness into the limitations
field; reserve that field for the review's evidence, parser, scope, or verification limits.

COMPLETE PAPER CORPUS
${corpus}

ADJUDICATED FINAL CONCERNS
${concerns}
