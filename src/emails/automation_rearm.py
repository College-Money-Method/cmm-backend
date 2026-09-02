"""Clear a webinar's automation send ledger so a moved session mails again.

``AutomationSendLedger`` row existence *is* "already sent" (see that model and
``automation_runner``): the runner excludes any ``portal_mapping`` that already
has a row for an automation. That is exactly right while a session's date is
fixed, and silently wrong the moment it moves — a reminder sent for the old
date can never be re-sent for the new one, so counselors keep an obsolete date
and nobody is told.

Re-arming deletes those rows for the webinar's school mappings, which puts
every automation back in the runner's due-window evaluation against the new
``start_datetime``. Only the ledger is touched: ``email_send_log`` is the audit
trail of what actually went out and is not used for dedupe, so it stays.

Lives here rather than in the workshops router because the ledger belongs to
the email side; the router only says *when* a re-arm is warranted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.emails.automation_ledger_models import AutomationSendLedger
from src.workshops.models import PortalMapping


def rearm_automations_for_webinar(db: Session, webinar_id: uuid.UUID) -> int:
    """Forget every automation send recorded for this webinar's mappings.

    Returns the number of ledger rows cleared — one per (automation, school)
    pair that will be re-evaluated, which is also the upper bound on how many
    schools get mailed again.

    Does **not** commit. The caller commits it together with the datetime
    change, so a webinar can never end up moved while its automations still
    claim to have been sent for the old date. Both directions are cleared:
    leaving ``after`` rows in place would make a post-workshop follow-up that
    already went out for a session that has now not happened unrepeatable.
    """
    mapping_ids = select(PortalMapping.id).where(PortalMapping.webinar_id == webinar_id)
    result = db.execute(
        delete(AutomationSendLedger).where(AutomationSendLedger.portal_mapping_id.in_(mapping_ids))
    )
    return result.rowcount or 0
