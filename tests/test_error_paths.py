"""Coverage for how errors are built, classified and described.

Two of twenty-one exception classes were referenced anywhere before this, yet
nearly every bug worth finding surfaces through this machinery: the
``RequestValidatorList`` decode that turned a generic INVALID_ARGUMENT into a
field name, the HTTP-401-versus-gRPC-UNAUTHENTICATED distinction that located
the ``_optional_token`` bug, and the retryable/non-retryable split that decides
whether a failure is even worth reporting.

Nothing here needs a network: gRPC statuses and HTTP codes are mapped by
``make_grpc_error``/``make_http_error``, so the mapping can be checked directly.
"""

from __future__ import annotations

import inspect

import pytest

from rootpy import exceptions as E


class TestGrpcStatusMapping:
    """Each gRPC status must produce its own class, not a generic error."""

    CASES = [
        (1, "GrpcCancelled"), (2, "GrpcUnknown"), (3, "GrpcInvalidArgument"),
        (4, "GrpcDeadlineExceeded"), (5, "GrpcNotFound"),
        (6, "GrpcAlreadyExists"), (7, "GrpcPermissionDenied"),
        (8, "GrpcResourceExhausted"), (9, "GrpcFailedPrecondition"),
        (10, "GrpcAborted"), (11, "GrpcOutOfRange"), (12, "GrpcUnimplemented"),
        (13, "GrpcInternal"), (14, "GrpcUnavailable"), (15, "GrpcDataLoss"),
        (16, "GrpcUnauthenticated"),
    ]

    @pytest.mark.parametrize("status,expected", CASES)
    def test_status_maps_to_its_class(self, status, expected):
        error = E.make_grpc_error("root.X/Y", status)
        assert type(error).__name__ == expected

    @pytest.mark.parametrize("status,expected", CASES)
    def test_every_mapped_error_is_a_grpcweberror(self, status, expected):
        assert isinstance(E.make_grpc_error("root.X/Y", status), E.GrpcWebError)

    def test_status_zero_is_not_an_error_class_of_its_own(self):
        """0 is OK; it should never be constructed as a specific failure."""
        error = E.make_grpc_error("root.X/Y", 0)
        assert isinstance(error, E.GrpcWebError)

    def test_unknown_status_still_produces_an_error(self):
        assert isinstance(E.make_grpc_error("root.X/Y", 999), E.GrpcWebError)

    def test_string_statuses_are_accepted(self):
        """The transport reads grpc-status off a header, so it is a string."""
        assert isinstance(
            E.make_grpc_error("root.X/Y", "3"), E.GrpcInvalidArgument
        )

    def test_operation_is_preserved(self):
        error = E.make_grpc_error("root.CommunityGrpcService/Create", 3)
        assert "CommunityGrpcService" in str(error) or "CommunityCreate" in str(error)


class TestHttpStatusMapping:
    CASES = [
        (400, "BadRequest"), (401, "Unauthorized"), (403, "Forbidden"),
        (404, "HttpNotFound"), (409, "Conflict"), (413, "PayloadTooLarge"),
        (429, "RateLimited"), (502, "BadGateway"), (503, "ServiceUnavailable"),
        (504, "GatewayTimeout"),
    ]

    @pytest.mark.parametrize("code,expected", CASES)
    def test_code_maps_to_its_class(self, code, expected):
        assert type(E.make_http_error("op", code)).__name__ == expected

    @pytest.mark.parametrize("code,expected", CASES)
    def test_all_are_http_errors(self, code, expected):
        assert isinstance(E.make_http_error("op", code), E.RootError)

    def test_unmapped_code_still_produces_an_error(self):
        assert isinstance(E.make_http_error("op", 418), E.RootError)

    def test_401_is_distinct_from_grpc_unauthenticated(self):
        """This distinction located the _optional_token bug.

        An HTTP 401 means the request was rejected before gRPC -- typically no
        auth header at all. gRPC 16 means the header was present and refused.
        Collapsing them would have hidden which layer was at fault.
        """
        http = E.make_http_error("op", 401)
        grpc = E.make_grpc_error("op", 16)
        assert type(http) is not type(grpc)
        assert not isinstance(http, E.GrpcWebError)

    def test_server_errors_share_a_base(self):
        for code in (502, 503, 504):
            assert isinstance(E.make_http_error("op", code), E.ServerError)


class TestErrorHierarchy:
    """Callers catch by base class, so the bases have to be right."""

    def test_everything_descends_from_rooterror(self):
        for name in dir(E):
            obj = getattr(E, name)
            if inspect.isclass(obj) and issubclass(obj, Exception):
                if obj.__module__ != E.__name__:
                    continue
                assert issubclass(obj, E.RootError), f"{name} escapes RootError"

    def test_rooterror_is_an_exception(self):
        assert issubclass(E.RootError, Exception)

    def test_catching_the_base_catches_a_specific_grpc_error(self):
        with pytest.raises(E.RootError):
            raise E.make_grpc_error("root.X/Y", 3)

    def test_catching_the_base_catches_a_specific_http_error(self):
        with pytest.raises(E.RootError):
            raise E.make_http_error("op", 404)

    def test_email_already_exists_is_an_account_conflict(self):
        assert issubclass(E.EmailAlreadyExists, E.AccountAlreadyExists)


class TestErrorDescription:
    """``get_error_info``/``format_root_error`` are the diagnostic surface."""

    def test_get_error_info_returns_something_structured(self):
        info = E.get_error_info(E.make_grpc_error("root.X/Y", 3))
        assert info is not None

    def test_format_is_a_string(self):
        assert isinstance(
            E.format_root_error(E.make_grpc_error("root.X/Y", 5)), str
        )

    def test_verbose_format_is_at_least_as_detailed(self):
        error = E.make_grpc_error("root.X/Y", 5)
        assert len(E.format_root_error(error, verbose=True)) >= len(
            E.format_root_error(error)
        )

    def test_a_plain_exception_does_not_crash_the_formatter(self):
        assert isinstance(E.format_root_error(ValueError("nope")), str)

    def test_every_grpc_error_carries_a_description(self):
        for status, _ in TestGrpcStatusMapping.CASES:
            error = E.make_grpc_error("root.X/Y", status)
            assert error.description, f"status {status} has no description"

    def test_every_grpc_error_carries_a_hint(self):
        for status, _ in TestGrpcStatusMapping.CASES:
            error = E.make_grpc_error("root.X/Y", status)
            assert error.hint, f"status {status} has no hint"


class TestValidationDetailIsAlwaysReachable:
    """The decoded payload must be on the exception, not only in the message."""

    def _error(self):
        from .test_untested_surface import build_root_exception_header

        return E.make_grpc_error(
            "root.CommunityGrpcService/Create",
            3,
            "One or more request values were rejected by Root.",
            response_headers=build_root_exception_header(
                [("PictureHex", "must not be empty", "NotEmptyValidator")]
            ),
        )

    def test_validation_errors_are_exposed_as_data(self):
        assert len(self._error().validation_errors) == 1

    def test_property_name_is_readable(self):
        error = self._error()
        assert error.validation_errors[0].property_name == "PictureHex"

    def test_payload_kind_is_identified(self):
        assert self._error().payload_kind == "request_validator_list"

    def test_errors_without_a_payload_expose_an_empty_list(self):
        error = E.make_grpc_error("root.X/Y", 3)
        assert error.validation_errors == []

    def test_root_exception_is_none_without_a_header(self):
        assert E.make_grpc_error("root.X/Y", 3).root_exception is None


class TestSignupConflictIsTyped:
    """A taken username must raise the typed exception, not the bare gRPC one.

    ``AuthClient.signup`` guarded this branch with ``exc.status == "6"``.
    ``exc.status`` is a ``GrpcStatus`` IntEnum, so the comparison was never
    true and the whole mapping was dead code: ``UsernameAlreadyExists`` and
    ``EmailAlreadyExists`` are exported from ``rootpy`` and caught by
    ``AccountCreator``, and neither could ever be raised.

    Exactly the shape of bug the project keeps finding -- it does not throw, it
    quietly does the wrong thing -- and it is checkable offline, so it is
    checked offline.
    """

    def _signup(self, error, username="taken_name", email="taken@example.com"):
        import asyncio

        from rootpy.auth import AuthClient

        class _Transport:
            async def unary(self, **kwargs):
                raise error

        client = AuthClient(_Transport())
        return asyncio.run(client.signup(username, "pw", email))

    def _conflict(self, *, naming):
        """An ALREADY_EXISTS whose payload names ``naming``."""
        from .test_untested_surface import build_root_exception_header

        return E.make_grpc_error(
            "root.Connect.AuthGrpcService/PasswordSignUp",
            6,
            "The requested object or relationship already exists.",
            response_headers=build_root_exception_header(
                [(naming, "is already taken", "AlreadyExistsValidator")]
            ),
        )

    def test_status_six_is_not_the_string_six(self):
        """The premise, pinned: comparing the status to "6" can never match."""
        error = E.make_grpc_error("root.X/Y", 6)
        assert error.status == E.GrpcStatus.ALREADY_EXISTS
        assert error.status == 6
        assert not (error.status == "6"), (
            "GrpcStatus is an IntEnum; a string comparison silently never "
            "matches, which is what made the signup conflict mapping dead"
        )

    def test_a_taken_username_raises_username_already_exists(self):
        with pytest.raises(E.UsernameAlreadyExists):
            self._signup(self._conflict(naming="taken_name"))

    def test_a_taken_email_raises_email_already_exists(self):
        with pytest.raises(E.EmailAlreadyExists):
            self._signup(self._conflict(naming="taken@example.com"))

    def test_an_unattributable_conflict_still_raises_the_base_class(self):
        """No payload to read: still a signup conflict, just an unnamed one."""
        bare = E.make_grpc_error("root.Connect.AuthGrpcService/PasswordSignUp", 6)
        with pytest.raises(E.AccountAlreadyExists):
            self._signup(bare)

    def test_other_statuses_are_left_alone(self):
        """Only ALREADY_EXISTS is remapped; everything else propagates."""
        denied = E.make_grpc_error(
            "root.Connect.AuthGrpcService/PasswordSignUp", 7
        )
        with pytest.raises(E.GrpcPermissionDenied):
            self._signup(denied)
