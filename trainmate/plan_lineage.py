"""Walking a plan's blocks without tripping over superseded versions.

One rule, stated once for every caller that needs it: navigate periodization by
macrocycle id, never by a date-ordered mesocycle query (DESIGN_plan_rollback.md §6.1).
"""
from typing import Any, Dict, List, Optional, Sequence


def plan_lineage(
    dbh, macros: Sequence[Optional[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """The blocks of `macros`, flattened into the order the athlete trained them.

    `macros` may hold Nones and repeats — callers assemble it from several single-plan
    lookups — and both are dropped. Plans are ordered by their first block's start date,
    not by argument position: the plan being replaced can be for a later goal than the
    governing one. See DESIGN_plan_rollback.md §6.1 for why this walks by macrocycle id.
    """
    lineages: List[List[Dict[str, Any]]] = []
    seen = set()
    for macro in macros:
        if not macro or macro['id'] in seen:
            continue
        seen.add(macro['id'])
        blocks = dbh.get_mesocycles_for_macrocycle(macro['id'])
        if blocks:
            lineages.append(blocks)
    lineages.sort(key=lambda blocks: blocks[0]['start_date'])
    return [block for lineage in lineages for block in lineage]
