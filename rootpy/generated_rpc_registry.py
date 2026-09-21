"""Raw gRPC registry: service endpoints and flat message schemas.

Backs :mod:`rootpy.raw_api`. Same lazy-loading arrangement as
:mod:`rootpy.structured_registry` -- see ``rootpy/data/*.json`` for the data
and ``rootpy._registry_loader`` for the mapping type.
"""

from __future__ import annotations

from ._registry_loader import LazyRegistry

#: Service name -> {method: {request, response, method_type, endpoint}}.
RPC_SERVICES = LazyRegistry("rpc_services.json")

#: Message name -> flat field schema used by the raw encoder/decoder.
MESSAGE_SCHEMAS = LazyRegistry("message_schemas.json")

__all__ = ["RPC_SERVICES", "MESSAGE_SCHEMAS"]
