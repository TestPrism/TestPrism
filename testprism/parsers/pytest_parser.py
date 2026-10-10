import re
from enum import Enum

from testprism.parsers.base_parser import BaseParser, UnitTestStatus


class PytestTestStatus(Enum):
    # Used when the status is not known
    UNKNOWN = "unknown"

    # Pytest test statuses
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    XFAIL = "xfail"
    XPASS = "xpass"
    ERROR = "error"

    def to_test_status(self) -> UnitTestStatus:
        match self:
            case (
                PytestTestStatus.PASSED
                | PytestTestStatus.XFAIL
                | PytestTestStatus.SKIPPED
            ):
                return UnitTestStatus.PASSED
            case (
                PytestTestStatus.FAILED
                | PytestTestStatus.XPASS
                | PytestTestStatus.ERROR
                | PytestTestStatus.UNKNOWN
            ):
                return UnitTestStatus.FAILED
            case _:
                raise ValueError(f"Unknown pytest test status: {self}")


class PytestParser(BaseParser):
    ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    SHORT_TEST_SUMMARY_INFO_PATTERN = r"=+\s*short test summary info\s*=+"
    FAILED_DESCRIPTION_DELIMETER = " - "
    NODE_ID_PATTERN = r"\S+::\S+"
    SUMMARY_RESULT_PATTERN = re.compile(
        rf"^(?P<status>{'|'.join(PytestTestStatus.__members__)})\s+"
        rf"(?P<nodeid>{NODE_ID_PATTERN})(?:\s+-.*)?$"
    )
    VERBOSE_RESULT_PATTERN = re.compile(
        rf"^(?P<nodeid>{NODE_ID_PATTERN})\s+"
        rf"(?P<status>{'|'.join(PytestTestStatus.__members__)})"
        rf"(?:\s+\(.+\))?"
        rf"(?:\s+\[[^\]]+\])?$"
    )

    def _strip_ansi(self, content: str) -> str:
        return self.ANSI_ESCAPE_PATTERN.sub("", content)

    def _clean_line(self, line: str) -> str:
        if line.startswith(PytestTestStatus.FAILED.name):
            parts = line.split(self.FAILED_DESCRIPTION_DELIMETER)
            if len(parts) > 1:
                line = self.FAILED_DESCRIPTION_DELIMETER.join(parts[:-1])

        return line

    def _normalize_test_name(self, node_id: str) -> str:
        return node_id.split("::", maxsplit=1)[-1].strip()

    def _parse_result_line(self, line: str) -> tuple[str, PytestTestStatus]:
        cleaned_line = self._clean_line(line)
        match = self.SUMMARY_RESULT_PATTERN.match(cleaned_line.strip())
        if match is None:
            return "", PytestTestStatus.UNKNOWN

        status = match.group("status")
        if status not in PytestTestStatus.__members__:
            return "", PytestTestStatus.UNKNOWN

        test_name = self._normalize_test_name(match.group("nodeid"))

        if not test_name:
            return "", PytestTestStatus.UNKNOWN

        return test_name, PytestTestStatus[status]

    def _parse_verbose_result_line(self, line: str) -> tuple[str, PytestTestStatus]:
        match = self.VERBOSE_RESULT_PATTERN.match(line.strip())
        if match is None:
            return "", PytestTestStatus.UNKNOWN

        status = match.group("status")
        if status not in PytestTestStatus.__members__:
            return "", PytestTestStatus.UNKNOWN

        test_name = self._normalize_test_name(match.group("nodeid"))
        if not test_name:
            return "", PytestTestStatus.UNKNOWN

        return test_name, PytestTestStatus[status]

    def _parse_test_results(self, lines: list[str]) -> dict[str, UnitTestStatus]:
        results = {}

        for line in lines:
            test_name, status = self._parse_result_line(line)

            if status != PytestTestStatus.UNKNOWN:
                results[test_name] = status.to_test_status()

        return results

    def _parse_verbose_test_results(
        self, lines: list[str]
    ) -> dict[str, UnitTestStatus]:
        results = {}

        for line in lines:
            test_name, status = self._parse_verbose_result_line(line)
            if status != PytestTestStatus.UNKNOWN:
                results[test_name] = status.to_test_status()

        return results

    def parse(self, content: str) -> dict[str, UnitTestStatus]:
        cleaned_content = self._strip_ansi(content)
        results = self._parse_verbose_test_results(cleaned_content.splitlines())

        parts = re.split(
            pattern=self.SHORT_TEST_SUMMARY_INFO_PATTERN,
            string=cleaned_content,
            flags=re.IGNORECASE,
            maxsplit=1,
        )

        if len(parts) >= 2:
            short_test_summary = parts[1]
            results.update(
                self._parse_test_results(lines=short_test_summary.splitlines())
            )

        if results:
            return results

        if len(parts) < 2:
            raise ValueError(
                "No short test summary info found in the provided content."
            )

        raise ValueError("No pytest test results found in the provided content.")
