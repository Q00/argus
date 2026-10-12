Build the reverse outline of one section of a LaTeX manuscript. Work from the text as written now, not
from what the author planned. Each unit below is one paragraph, figure environment, or table environment
of the source, in reading order; its sentences are numbered S1, S2, ... within the unit. The units just
before and after the section are read-only context: return nothing for them.

Items. For every unit, state in one line what the reader newly learns there (learns). Then run the
deletion test: remove the unit and read the previous and next unit together. removable_without_loss is
true only when the flow and the argument still hold; lost_if_removed states what the reader would lose.
Counter-evidence that matters, conditions that limit a claim, and definitions that later text uses are
never removable. Repetition, defensive sentences, and numbers that only repeat a table are deletion
candidates. Choose one action: keep; fix (rewrite in place); delete (only when removable_without_loss is
true); move (move_before names the main-body unit ID or section ID that the unit should precede, and is
different from the unit itself; it is null for every other action). Never move content to an appendix or
a footnote to restore length. direction states the concrete change in one or two sentences, or why the
unit stays as it is.

Pairs. For every listed adjacent pair, read the two units together. takes_up_previous is true when the
second unit takes up the question or point the first one leaves the reader with. shifted lists which of
object (what is discussed), point (what is said about it), and criterion (how it is judged) change
between the two; a pair where all three change at once is a jump. In note, say whether the relation
between the two still holds without its connective (so, because, thus).

Sentences. Label every numbered sentence of every unit with kind: causal_link when the sentence asserts
a cause, consequence, or inference, with or without an explicit connective; number when it states a
numeric value, count, or comparison of numbers; causal_link_and_number when both hold; none otherwise.
For any kind other than none, copy into evidence_span the exact words of the sentence that carry the
causal link or the number, character for character, with no paraphrase; for none, evidence_span is
null. For a causal link, connective_holds says whether the stated relation follows from what the
manuscript shows; it is null when the kind has no causal link. Claim ledger: support_cells lists the
IDs of the table cells that back the sentence, using only IDs from the table cell catalog. verdict is
supported when the listed cells back the sentence as written; narrow_scope when the cells back only a
narrower claim or no cell backs it, and then note states the narrower claim the cells do support;
not_applicable exactly when kind is none.

Wording mode for this call: ${wording_mode}. In normal mode, wording_issue is fact_error when the
sentence misstates what the manuscript or its tables show, actor_not_subject when the acting entity is
not the grammatical subject, new_before_given when new information comes before the given information it
builds on, and none otherwise. In preserve-wording mode the sentences are final (for example a
coauthor's text): report only fact_error, and none otherwise. wording_suggestion is the corrected
sentence whenever wording_issue is not none, and null otherwise.

Concerns. For every listed final review concern located in this section, direction states the smallest
change to the text that answers it: fix, delete, move, or narrow a claim. needs_new_computation is true
when an answer needs a new experiment, run, or computation; it is a flag for the author's decision,
never a plan to run anything.

Return exactly ${unit_count} items, ${pair_count} pairs, ${sentence_count} sentences, and ${concern_count}
concerns. Each unit ID, adjacent pair, (unit_id, sentence_ordinal) pair, and concern ID appears exactly
once. Keep every field terse so the complete coverage fits in one response.

SECTION
${section}

MAIN BODY ORDER
${main_order}

CONTEXT UNITS
${context}

ADJACENT PAIRS
${pairs}

SECTION CONCERNS
${concerns}

TABLE CELL CATALOG
${catalog}

SECTION UNITS
${units}
