import logging
import re

from testprism.parsers.base_parser import BaseParser, UnitTestStatus

logger = logging.getLogger(__name__)


class SWEPolyBenchParser(BaseParser):
    START_MARKER = r"SWEPolyBench results starts here"
    END_MARKER = r"SWEPolyBench results ends here"
    PASS_STATUSES = {"PAS", "PASS", "PASSED"}
    FAIL_STATUSES = {"FAI", "FAIL", "FAILED"}
    FULL_STATUSES = {"PASSED", "FAILED"}
    _STACK_FRAME_RE = re.compile(r"(?:^|[A-Za-z0-9_$.])\.java:\d+\)")

    def parse(self, content: str) -> dict[str, UnitTestStatus]:
        if self.START_MARKER not in content or self.END_MARKER not in content:
            # sometimes the server is unable to fetch the git repository.
            # see swepolybench issue here: https://github.com/amazon-science/SWE-PolyBench/issues/400
            raise ValueError(
                "Couldn't find SWEPolyBench results "
                "between the start/end markers. "
                "Skipping this task as it is an "
                "issue from the server end."
            )

        content = content.split(self.START_MARKER, 1)[-1]
        content = content.rsplit(self.END_MARKER, 1)[0]

        block = content.strip()
        results: dict[str, UnitTestStatus] = {}

        lines = self._logical_result_lines(block)
        if len(lines) == 1 and lines[0].upper() in self.PASS_STATUSES | self.FAIL_STATUSES:
            results["tests"] = (
                UnitTestStatus.PASSED
                if lines[0].upper() in self.PASS_STATUSES
                else UnitTestStatus.FAILED
            )
            return results

        for line in lines:
            parsed = self._split_result_line(line)
            if parsed is None:
                raise ValueError(f"Unrecognized SWEPolyBench result line: {line!r}")
            test_id, status = parsed
            normalized = status.strip().upper()
            if normalized in self.PASS_STATUSES:
                results[test_id.strip()] = UnitTestStatus.PASSED
            elif normalized in self.FAIL_STATUSES:
                results[test_id.strip()] = UnitTestStatus.FAILED
            else:
                raise ValueError(f"Unrecognized SWEPolyBench status: {status!r}")
        return results

    def _logical_result_lines(self, block: str) -> list[str]:
        physical_lines = [
            line
            for raw_line in block.splitlines()
            if (line := self._clean_physical_line(raw_line))
        ]
        logical_lines: list[str] = []
        i = 0
        while i < len(physical_lines):
            candidate = physical_lines[i]
            while i + 1 < len(physical_lines) and self._should_join_next(
                candidate,
                physical_lines[i + 1],
            ):
                i += 1
                candidate += self._join_separator(candidate, physical_lines[i])
                candidate += physical_lines[i]
            logical_lines.append(candidate)
            i += 1
        return logical_lines

    def _clean_physical_line(self, raw_line: str) -> str | None:
        line = raw_line.strip()
        if not line:
            return None
        if ">" in line:
            suffix = line.rsplit(">", 1)[1].strip()
            if not suffix:
                return None
            line = suffix
        if self._is_noise_line(line):
            return None
        return line

    def _is_noise_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        upper = stripped.upper()
        if upper in self.PASS_STATUSES | self.FAIL_STATUSES:
            return False
        if stripped.startswith(":") or stripped.endswith((" :", "::")):
            return False
        if " :: " in stripped:
            return False
        if stripped.startswith(("<", "[", "at ", "Caused by:", "Suppressed:")):
            return True
        if "<" in stripped or "</" in stripped:
            return True
        if self._STACK_FRAME_RE.search(stripped):
            return True
        return False

    def _join_separator(self, candidate: str, next_line: str) -> str:
        next_status = next_line.strip().upper()
        if candidate.endswith(("::", ":")) and next_status in self.PASS_STATUSES | self.FAIL_STATUSES:
            return " "
        return ""

    def _should_join_next(self, candidate: str, next_line: str) -> bool:
        if not self._has_complete_status(candidate):
            return True
        status = self._status_fragment(candidate)
        if status is None or status.strip().upper() in self.FULL_STATUSES:
            return False
        if self._has_complete_status(next_line):
            return False
        return self._has_complete_status(candidate + next_line)

    def _has_complete_status(self, line: str) -> bool:
        status = self._status_fragment(line)
        if status is None:
            return False
        return status.strip().upper() in self.PASS_STATUSES | self.FAIL_STATUSES

    def _split_result_line(self, line: str) -> tuple[str, str] | None:
        if " :: " in line:
            test_id, status = line.rsplit(" :: ", 1)
        elif ":" in line:
            test_id, status = line.rsplit(":", 1)
        else:
            return None
        if not test_id.strip():
            return None
        return test_id, status

    def _status_fragment(self, line: str) -> str | None:
        parsed = self._split_result_line(line)
        if parsed is not None:
            return parsed[1].strip()
        if line.strip().isalpha():
            return line.strip()
        return None
