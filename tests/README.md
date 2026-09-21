# rootpy tests

Every test here is offline. Nothing needs a token, a network connection or a
Root account:

    pip install -e ".[dev]"
    python -m pytest -q

Roughly half the suite is ordinary unit tests — permission algebra, pagination,
enum helpers, model properties, wire encoding and decoding. The other half is
**guards**: sweeps over the package's own AST that pin a rule the code has to
keep obeying. No `**kwargs` on a public manager, no `read_text()` without an
encoding, every `high.*` call site naming a field the schema actually has,
every `client.*` call in `examples/` and the documentation matching its real
signature and its real sync/async-ness.

A guard is the cheap way to catch a defect whose only other symptom is a
puzzling rejection from the server, so prefer adding one to adding a comment.

## Wire rule for new RPC methods

Pass the raw protobuf body to `transport.unary(...)` and parse responses with
`unwrap_grpc_web(...)`. Do not frame by hand and do not read
`response.content` directly.

Every gRPC-web request body needs a 5-byte frame: one flag byte, then a
big-endian length. An unframed body makes the server's handler throw a bare
`UNKNOWN (2) Exception was thrown by handler`, which is indistinguishable from
a permissions failure. Framing happens inside `GrpcWebTransport.unary` so that
no call site can forget, and `test_wire.py` keeps it that way.

## Adding a test

Match the file it belongs in — `test_wire.py` for framing and field numbers,
`test_protocol_schemas.py` for packet shapes, `test_untested_surface.py` for
the guards, `test_features.py` for behaviour. Say in the docstring what goes
wrong when the thing being pinned is wrong; that is what makes a failure
readable a year later.
