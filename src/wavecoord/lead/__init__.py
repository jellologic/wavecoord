"""The lead tools (P4): plan, dispatch, collect, conflicts, resolve and the refinery.

`lead_for(coord)` gives the one `Lead` of a coordinator. MCP and REST call it
with the lead role; see docs/contract.md section 8.
"""

import threading

from .base import LeadBase, Refused, lconst  # noqa: F401
from .collect import CollectMixin
from .refinery import RefineryMixin

_LOCK = threading.Lock()


class Lead(CollectMixin, RefineryMixin, LeadBase):
    pass


def lead_for(coord):
    with _LOCK:
        lead = getattr(coord, "_lead", None)
        if lead is None:
            lead = Lead(coord)
            coord._lead = lead
        return lead
