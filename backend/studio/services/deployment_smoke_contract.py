from __future__ import annotations

from studio.services.async_media_contract import ASYNC_MEDIA_OPERATIONS
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.script_generation_contract import SCRIPT_GENERATE_OPERATION
from studio.services.script_structure import SCRIPT_STRUCTURE_OPERATION
from studio.services.production_visual_rules import VISUAL_RULES_OPERATION


TEXT_DEPLOYMENT_SMOKE_OPERATIONS = frozenset({
    PROMPT_NORMALIZE_OPERATION,
    SCRIPT_GENERATE_OPERATION,
    SCRIPT_STRUCTURE_OPERATION,
    VISUAL_RULES_OPERATION,
})

DEPLOYMENT_SMOKE_OPERATIONS = frozenset(ASYNC_MEDIA_OPERATIONS) | TEXT_DEPLOYMENT_SMOKE_OPERATIONS

# A worker may cross the external POST boundary only once.  If it loses the
# response while ``submitting``, a human must append evidence before another
# attempt can be created.
DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS = "durable_single_submit_manual_resolution"
