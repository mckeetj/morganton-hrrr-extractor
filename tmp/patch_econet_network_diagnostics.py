from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"expected block not found: {label}")
    return text.replace(old, new, 1)


econet_path = Path("burke_hrrr/econet.py")
econet = econet_path.read_text(encoding="utf-8")

econet = replace_once(
    econet,
    "import csv\nimport datetime as dt\nimport io\nimport json\nimport math\nimport os\nimport statistics\nimport sys\nimport time\n",
    "import csv\nimport datetime as dt\nimport errno\nimport io\nimport json\nimport math\nimport os\nimport socket\nimport ssl\nimport statistics\nimport sys\nimport time\n",
    "econet imports",
)

fetch_marker = '''def _fetch_csv(api_hash: str, start: dt.datetime, end: dt.datetime, retries: int = 4) -> str:\n'''
classifier = '''def _classify_url_error(exc: urllib.error.URLError) -> str:\n    """Return a safe, actionable network diagnostic without exposing the request URL.\n\n    The CLOUDS request URL contains the private API hash, so this function never\n    stringifies the URLError or its reason. Only exception types and numeric errno\n    values are inspected.\n    """\n    reason = exc.reason\n    err_no = getattr(reason, "errno", None)\n\n    if isinstance(reason, socket.gaierror):\n        return "CLOUDS API DNS resolution failed"\n    if isinstance(reason, ssl.SSLCertVerificationError):\n        return "CLOUDS API TLS certificate verification failed"\n    if isinstance(reason, ssl.SSLError):\n        return "CLOUDS API TLS/SSL connection failed"\n    if isinstance(reason, (TimeoutError, socket.timeout)):\n        return "CLOUDS API connection timed out"\n    if isinstance(reason, ConnectionRefusedError) or err_no == errno.ECONNREFUSED:\n        return "CLOUDS API connection was refused"\n    if isinstance(reason, ConnectionResetError) or err_no == errno.ECONNRESET:\n        return "CLOUDS API connection was reset"\n    if isinstance(reason, ConnectionAbortedError) or err_no == errno.ECONNABORTED:\n        return "CLOUDS API connection was aborted"\n    if err_no in {errno.ENETUNREACH, errno.EHOSTUNREACH}:\n        return "CLOUDS API network/host was unreachable"\n\n    return "CLOUDS API network request failed (unclassified connection error)"\n\n\n'''
econet = replace_once(econet, fetch_marker, classifier + fetch_marker, "fetch marker")

econet = replace_once(
    econet,
    '''        except urllib.error.URLError:\n            last_error = RuntimeError("CLOUDS API network request failed")\n            if attempt == retries - 1:\n                raise last_error from None\n        except (TimeoutError, UnicodeDecodeError):\n            last_error = RuntimeError("CLOUDS API response timed out or was not valid UTF-8 CSV")\n            if attempt == retries - 1:\n                raise last_error from None\n''',
    '''        except urllib.error.URLError as exc:\n            # Never stringify exc/reason: the request URL contains the secret hash.\n            last_error = RuntimeError(_classify_url_error(exc))\n            if attempt == retries - 1:\n                raise last_error from None\n        except TimeoutError:\n            last_error = RuntimeError("CLOUDS API connection timed out")\n            if attempt == retries - 1:\n                raise last_error from None\n        except UnicodeDecodeError:\n            last_error = RuntimeError("CLOUDS API response was not valid UTF-8 CSV")\n            if attempt == retries - 1:\n                raise last_error from None\n''',
    "URL error handler",
)

econet_path.write_text(econet, encoding="utf-8")


test_path = Path("tests/test_econet.py")
tests = test_path.read_text(encoding="utf-8")

tests = replace_once(
    tests,
    "import datetime as dt\nimport unittest\nfrom zoneinfo import ZoneInfo\n",
    "import datetime as dt\nimport errno\nimport socket\nimport ssl\nimport unittest\nimport urllib.error\nfrom unittest.mock import patch\nfrom zoneinfo import ZoneInfo\n",
    "test imports",
)

tests = replace_once(
    tests,
    '''    Observation,\n    _build_url,\n    build_summary,\n''',
    '''    Observation,\n    _build_url,\n    _classify_url_error,\n    _fetch_csv,\n    build_summary,\n''',
    "test econet imports",
)

insert_before = '''    def test_parse_standard_csv_long_records(self) -> None:\n'''
new_tests = '''    def test_url_error_diagnostics_are_specific_and_sanitized(self) -> None:\n        cases = [\n            (urllib.error.URLError(socket.gaierror(-2, "SECRET_HASH")), "CLOUDS API DNS resolution failed"),\n            (urllib.error.URLError(TimeoutError("SECRET_HASH")), "CLOUDS API connection timed out"),\n            (urllib.error.URLError(ssl.SSLCertVerificationError("SECRET_HASH")), "CLOUDS API TLS certificate verification failed"),\n            (urllib.error.URLError(ssl.SSLError("SECRET_HASH")), "CLOUDS API TLS/SSL connection failed"),\n            (urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "SECRET_HASH")), "CLOUDS API connection was refused"),\n            (urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "SECRET_HASH")), "CLOUDS API connection was reset"),\n            (urllib.error.URLError(OSError(errno.ENETUNREACH, "SECRET_HASH")), "CLOUDS API network/host was unreachable"),\n            (urllib.error.URLError(OSError(9999, "SECRET_HASH")), "CLOUDS API network request failed (unclassified connection error)"),\n        ]\n        for exc, expected in cases:\n            with self.subTest(expected=expected):\n                diagnostic = _classify_url_error(exc)\n                self.assertEqual(diagnostic, expected)\n                self.assertNotIn("SECRET_HASH", diagnostic)\n                self.assertNotIn("hash=", diagnostic.lower())\n\n    def test_fetch_csv_surfaces_sanitized_dns_failure(self) -> None:\n        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)\n        failure = urllib.error.URLError(socket.gaierror(-2, "SECRET_HASH"))\n        with patch("burke_hrrr.econet.urllib.request.urlopen", side_effect=failure):\n            with self.assertRaises(RuntimeError) as caught:\n                _fetch_csv("SECRET_HASH", now - dt.timedelta(days=7), now, retries=1)\n        message = str(caught.exception)\n        self.assertEqual(message, "CLOUDS API DNS resolution failed")\n        self.assertNotIn("SECRET_HASH", message)\n\n    def test_fetch_csv_distinguishes_direct_timeout(self) -> None:\n        now = dt.datetime(2026, 8, 21, 10, 15, tzinfo=EASTERN)\n        with patch("burke_hrrr.econet.urllib.request.urlopen", side_effect=TimeoutError("SECRET_HASH")):\n            with self.assertRaises(RuntimeError) as caught:\n                _fetch_csv("SECRET_HASH", now - dt.timedelta(days=7), now, retries=1)\n        self.assertEqual(str(caught.exception), "CLOUDS API connection timed out")\n        self.assertNotIn("SECRET_HASH", str(caught.exception))\n\n'''
tests = replace_once(tests, insert_before, new_tests + insert_before, "test insertion marker")

test_path.write_text(tests, encoding="utf-8")

print("Patched burke_hrrr/econet.py and tests/test_econet.py")
