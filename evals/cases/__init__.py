"""Case modules. Order here is the order in the report."""
from evals.cases import (adversarial, class_spec, discovery_fetch, drafts_feedback, killswitch_cost, poc, qualification,  # noqa: F401
                         run_recovery, sending_gmail, storage_report)

ALL = [run_recovery, discovery_fetch, qualification, poc, drafts_feedback, sending_gmail, killswitch_cost, storage_report,
       adversarial, class_spec]
