# rootpy tests

Run with:

    python -m pytest tests/ -q

These are regression tests for bugs that were expensive to find. In particular:

**`test_wire.py` — request framing.** Every gRPC-web request body needs a
5-byte frame (flag + big-endian length). Three methods once sent unframed
bodies; the server's handler threw a bare `UNKNOWN (2) Exception was thrown by
handler`, which is indistinguishable from a permissions failure and cost a long
debugging detour. Framing now happens inside `GrpcWebTransport.unary`, so no
call site can forget, and these tests keep it that way.

**Wire rule for new RPC methods:** pass the raw protobuf body to
`transport.unary(...)` and parse responses with `unwrap_grpc_web(...)`. Don't
frame by hand and don't parse `response.content` directly.
