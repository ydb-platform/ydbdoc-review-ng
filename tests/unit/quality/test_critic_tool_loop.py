"""Unit tests for critic tool-loop FSM (P1b)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from ydbdoc_review_ng.quality.tool_loop import (
    CriticToolLoop,
    LoopFailureReason,
    ProtocolError,
    ToolCall,
    ToolLoopResult,
)
from ydbdoc_review_ng.quality.workspace import CriticWorkspace


def _workspace(draft: bytes = b"alpha\nbeta\ngamma\n") -> CriticWorkspace:
    return CriticWorkspace(
        writable_path="docs/en/article.md",
        writable_bytes=draft,
        read_only={"docs/ru/article.md": "источник\n".encode()},
    )


@dataclass
class _StubTurn:
    tool_calls: list[ToolCall]


class _StubModel:
    def __init__(self, turns: list[_StubTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0
        self.histories: list[list[dict[str, object]]] = []

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> _StubTurn:
        del tools
        self.histories.append(list(messages))
        if self.calls >= len(self._turns):
            raise AssertionError("stub model out of turns")
        turn = self._turns[self.calls]
        self.calls += 1
        return turn


def _call(name: str, arguments: dict[str, object], *, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def test_patch_without_covering_read_then_finish_is_protocol_error() -> None:
    model = _StubModel(
        [
            _StubTurn(
                [
                    _call(
                        "apply_patch",
                        {
                            "path": "docs/en/article.md",
                            "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                        },
                    )
                ]
            ),
            _StubTurn([_call("finish", {}, call_id="c2")]),
        ]
    )
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    with pytest.raises(ProtocolError):
        # Drive one patch, then finish without read via handle_turn sequencing.
        loop.handle_turn(model._turns[0].tool_calls)
        loop.handle_turn(model._turns[1].tool_calls)


def test_grep_while_pending_reread_is_protocol_error() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    loop.handle_turn(
        [
            _call(
                "apply_patch",
                {
                    "path": "docs/en/article.md",
                    "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                },
            )
        ]
    )
    with pytest.raises(ProtocolError):
        loop.handle_turn([_call("grep", {"pattern": "BETA", "path": "docs/en/article.md"})])


def test_second_apply_patch_while_pending_reread_is_protocol_error() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    loop.handle_turn(
        [
            _call(
                "apply_patch",
                {
                    "path": "docs/en/article.md",
                    "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                },
            )
        ]
    )
    with pytest.raises(ProtocolError):
        loop.handle_turn(
            [
                _call(
                    "apply_patch",
                    {
                        "path": "docs/en/article.md",
                        "patch": "@@\n alpha\n-BETA\n+beta\n gamma\n",
                    },
                    call_id="c2",
                )
            ]
        )


def test_apply_patch_bundled_with_other_tool_is_protocol_error() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    with pytest.raises(ProtocolError):
        loop.handle_turn(
            [
                _call(
                    "apply_patch",
                    {
                        "path": "docs/en/article.md",
                        "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                    },
                ),
                _call(
                    "read",
                    {"path": "docs/en/article.md", "start_line": 1, "end_line": 1},
                    call_id="c2",
                ),
            ]
        )


def test_parallel_read_grep_ok_when_pending_empty() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    results = loop.handle_turn(
        [
            _call("read", {"path": "docs/en/article.md", "start_line": 1, "end_line": 1}),
            _call(
                "grep",
                {"pattern": "beta", "path": "docs/en/article.md"},
                call_id="c2",
            ),
        ]
    )
    assert len(results) == 2
    assert "1|alpha" in results[0].content
    assert "beta" in results[1].content


def test_recoverable_tool_error_is_returned_not_session_abort() -> None:
    """Live DeepSeek often overshoots end_line; soft error must not RED the chunk."""
    loop = CriticToolLoop(_workspace(b"only\n"), max_tool_turns=12)
    bad = loop.handle_turn(
        [_call("read", {"path": "docs/en/article.md", "start_line": 1, "end_line": 40})]
    )
    payload = json.loads(bad[0].content)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_args"
    assert "EOF" in payload["detail"]
    # Session stays open; a valid follow-up still works.
    ok = loop.handle_turn(
        [
            _call(
                "read",
                {"path": "docs/en/article.md", "start_line": 1, "end_line": 1},
                call_id="c2",
            )
        ]
    )
    assert "1|only" in ok[0].content
    finish = loop.handle_turn([_call("finish", {}, call_id="c3")])
    assert finish[-1].name == "finish"
    assert loop.finished


def test_finish_with_pending_reread_is_protocol_error() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    loop.handle_turn(
        [
            _call(
                "apply_patch",
                {
                    "path": "docs/en/article.md",
                    "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                },
            )
        ]
    )
    with pytest.raises(ProtocolError):
        loop.handle_turn([_call("finish", {})])


def test_covering_read_clears_pending_and_finish_publishes_bytes() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=12)
    loop.handle_turn(
        [
            _call(
                "apply_patch",
                {
                    "path": "docs/en/article.md",
                    "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                },
            )
        ]
    )
    loop.handle_turn(
        [
            _call(
                "read",
                {"path": "docs/en/article.md", "start_line": 2, "end_line": 2},
                call_id="c2",
            )
        ]
    )
    outcome = loop.handle_turn([_call("finish", {}, call_id="c3")])
    assert outcome[-1].name == "finish"
    assert loop.finished
    assert loop.workspace.writable_bytes() == b"alpha\nBETA\ngamma\n"


def test_turn_budget_exceeded() -> None:
    loop = CriticToolLoop(_workspace(), max_tool_turns=2)
    loop.handle_turn(
        [_call("read", {"path": "docs/en/article.md", "start_line": 1, "end_line": 1})]
    )
    loop.handle_turn(
        [_call("grep", {"pattern": "a", "path": "docs/en/article.md"}, call_id="c2")]
    )
    with pytest.raises(ProtocolError) as caught:
        loop.handle_turn(
                [
                    _call(
                        "read",
                        {"path": "docs/ru/article.md", "start_line": 1, "end_line": 1},
                        call_id="c3",
                    )
                ]
            )
    assert caught.value.reason is LoopFailureReason.TURN_BUDGET


def test_run_retries_restart_from_original_draft() -> None:
    draft = b"alpha\nbeta\ngamma\n"

    class FlakyThenOk:
        def __init__(self) -> None:
            self.session = 0

        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> _StubTurn:
            del messages, tools
            # First session: patch then illegally finish (protocol → retry).
            # Second session: patch, covering read, finish.
            if self.session == 0:
                # Driven by CriticToolLoop.run which pulls one turn at a time.
                raise AssertionError("use scripted sequence via run helper")

    # Use explicit run with a callable that returns turns from a queue per attempt.
    attempts: list[list[_StubTurn]] = [
        [
            _StubTurn(
                [
                    _call(
                        "apply_patch",
                        {
                            "path": "docs/en/article.md",
                            "patch": "@@\n alpha\n-beta\n+WRONG\n gamma\n",
                        },
                    )
                ]
            ),
            _StubTurn([_call("finish", {}, call_id="c2")]),
        ],
        [
            _StubTurn(
                [
                    _call(
                        "apply_patch",
                        {
                            "path": "docs/en/article.md",
                            "patch": "@@\n alpha\n-beta\n+BETA\n gamma\n",
                        },
                    )
                ]
            ),
            _StubTurn(
                [
                    _call(
                        "read",
                        {"path": "docs/en/article.md", "start_line": 2, "end_line": 2},
                        call_id="c2",
                    )
                ]
            ),
            _StubTurn([_call("finish", {}, call_id="c3")]),
        ],
    ]
    attempt_index = {"n": 0}
    turn_index = {"n": 0}

    def complete(
        messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> _StubTurn:
        del messages, tools
        turns = attempts[attempt_index["n"]]
        turn = turns[turn_index["n"]]
        turn_index["n"] += 1
        return turn

    def on_retry() -> None:
        attempt_index["n"] += 1
        turn_index["n"] = 0

    result = CriticToolLoop.run_with_retry(
        workspace_factory=lambda: _workspace(draft),
        complete=complete,
        max_tool_turns=12,
        on_session_retry=on_retry,
    )
    assert isinstance(result, ToolLoopResult)
    assert result.ok
    assert result.reviewed_bytes == b"alpha\nBETA\ngamma\n"
    assert attempt_index["n"] == 1
