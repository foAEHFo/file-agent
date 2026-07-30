from __future__ import annotations

import unittest


class ToolResultTests(unittest.TestCase):
    def test_success_result_uses_the_shared_tool_contract(self) -> None:
        from file_agent.types import ToolResult

        result = ToolResult.success(
            summary="Found 2 matching files.",
            data={"paths": ["a.md", "b.md"]},
        )

        self.assertEqual(
            result.to_dict(),
            {
                "ok": True,
                "summary": "Found 2 matching files.",
                "data": {"paths": ["a.md", "b.md"]},
                "error": None,
            },
        )

    def test_failure_result_serializes_a_stable_error_code(self) -> None:
        from file_agent.types import ErrorCode, ToolResult

        result = ToolResult.failure(
            code=ErrorCode.FILE_NOT_FOUND,
            message="File does not exist.",
            summary="Unable to read missing.md.",
            details={"path": "missing.md"},
        )

        self.assertEqual(
            result.to_dict(),
            {
                "ok": False,
                "summary": "Unable to read missing.md.",
                "data": {},
                "error": {
                    "code": "FILE_NOT_FOUND",
                    "message": "File does not exist.",
                    "details": {"path": "missing.md"},
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
