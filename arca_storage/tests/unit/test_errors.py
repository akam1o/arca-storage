"""Tests for the structured error model."""


from arca_storage.errors import (
    AlreadyExistsError,
    ArcaError,
    ConflictError,
    ErrorCode,
    InvalidArgumentError,
    NotFoundError,
    ReconcileFailedError,
    SubprocessError,
    TimeoutError as ArcaTimeoutError,
    UnauthorizedError,
)


class TestErrorCodes:
    def test_not_found_http_status(self):
        err = NotFoundError("Volume", "svm1/vol1")
        assert err.http_status == 404
        assert err.code == ErrorCode.NOT_FOUND

    def test_already_exists_http_status(self):
        err = AlreadyExistsError("SVM", "prod")
        assert err.http_status == 409
        assert err.code == ErrorCode.ALREADY_EXISTS

    def test_unauthorized_http_status(self):
        err = UnauthorizedError()
        assert err.http_status == 401
        assert err.code == ErrorCode.UNAUTHORIZED

    def test_conflict_http_status(self):
        err = ConflictError("Volume is in use")
        assert err.http_status == 409

    def test_invalid_argument_http_status(self):
        err = InvalidArgumentError("name", "invalid!")
        assert err.http_status == 400

    def test_timeout_http_status(self):
        err = ArcaTimeoutError("lvcreate", 30)
        assert err.http_status == 504

    def test_subprocess_error(self):
        err = SubprocessError(["lvcreate"], 1, "error msg")
        assert err.http_status == 500
        assert err.message == "Command failed (rc=1)"
        assert err.to_dict()["details"] == {"returncode": 1}
        assert err.cmd == ["lvcreate"]
        assert err.stderr == "error msg"

    def test_reconcile_failed_error(self):
        err = ReconcileFailedError("Volume", "svm1/vol1", "Step 'mounted' failed: mount failed")
        assert err.http_status == 500
        assert err.code == ErrorCode.INTERNAL
        assert err.message == "Volume 'svm1/vol1' reconcile failed"
        assert err.details == {
            "resource": "Volume",
            "name": "svm1/vol1",
            "reason": "Step 'mounted' failed: mount failed",
        }

    def test_to_dict(self):
        err = NotFoundError("SVM", "test")
        d = err.to_dict()
        assert d["code"] == "NOT_FOUND"
        assert "message" in d
        assert "details" in d

    def test_arca_error_is_exception(self):
        err = NotFoundError("X", "Y")
        assert isinstance(err, Exception)
        assert isinstance(err, ArcaError)
