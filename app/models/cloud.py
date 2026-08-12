"""Marker base class for providers that send prompts to a third party.

The distinction matters to the policy engine, not to the agents. Anything
deriving from :class:`CloudModelProvider` is, by definition, outside the trust
boundary: its output is untrusted input and it can never back a principal that
holds privileged capability.
"""

from __future__ import annotations

from app.models.base import BaseModelProvider


class CloudModelProvider(BaseModelProvider):
    """A model hosted by a third party.

    Subclasses must keep two promises:

    * never log, echo, or return the API key;
    * never treat provider output as anything but untrusted text.
    """

    #: Read by :mod:`app.policy.engine`. There is no way to flip this per-instance.
    is_cloud: bool = True
