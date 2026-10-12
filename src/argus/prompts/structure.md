Check the section-level structure of a LaTeX manuscript from its reverse outline. Work from the text as
written now, not from what the author planned. The outline lists every section block in reading order
with what the reader learns in each unit and the action already chosen for it.

Sections. For every section, start_state is the question the reader holds on entering it,
arrival_state is the answer the section gives, and next_question is the question it leaves for what
follows. takes_up_previous_question is true when the section takes up the question the previous
section left; the first section takes up the question the paper itself raises and may be true by
definition. Run the deletion test on the whole section: removable_without_loss is true only when the
argument of the paper still holds without it. Choose one action: keep; fix; delete (only when
removable_without_loss is true); move (move_after names the section ID it should follow, different
from the section itself; it is null for every other action). Never move a section to the appendix or a
footnote to restore length. reason states why in one or two sentences.

Issues. For every external issue, unit_ids lists the main-body unit IDs the issue concerns (empty when
it concerns none of them). direction states the smallest change to the text that answers it: fix,
delete, move, or narrow a claim. needs_new_computation is true when an answer needs a new experiment,
run, or computation; it is a flag for the author's decision, never a plan to run anything. note states
any condition on the direction.

Return exactly ${section_count} sections and ${issue_count} issues, each ID exactly once. Use only the
section IDs and unit IDs given below.

SECTION OUTLINE
${outline}

FINAL REVIEW CONCERNS
${concerns}

EXTERNAL ISSUES
${issues}

MAIN BODY BY UNIT
${body}
