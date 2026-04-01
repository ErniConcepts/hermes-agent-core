from agent.reasoning_stream import ReasoningStreamMux, strip_reasoning_blocks


def test_strip_reasoning_blocks_removes_inline_think_tags() -> None:
    content = "<think>internal plan</think>\nVisible answer"
    assert strip_reasoning_blocks(content) == "Visible answer"


def test_strip_reasoning_blocks_handles_reasoning_scratchpad() -> None:
    content = "<REASONING_SCRATCHPAD>hidden</REASONING_SCRATCHPAD>Visible answer"
    assert strip_reasoning_blocks(content) == "Visible answer"


def test_reasoning_stream_mux_routes_reasoning_and_answer_chunks() -> None:
    answers: list[str] = []
    reasoning: list[str] = []
    mux = ReasoningStreamMux(on_answer=answers.append, on_reasoning=reasoning.append)

    mux.feed("<think>I should inspect the workspace</think>Done.")
    mux.flush()

    assert "".join(reasoning) == "I should inspect the workspace"
    assert "".join(answers) == "Done."


def test_reasoning_stream_mux_handles_split_tags_across_chunks() -> None:
    answers: list[str] = []
    reasoning: list[str] = []
    mux = ReasoningStreamMux(on_answer=answers.append, on_reasoning=reasoning.append)

    mux.feed("<thi")
    mux.feed("nk>Thinking")
    mux.feed("</th")
    mux.feed("ink>Visible")
    mux.flush()

    assert "".join(reasoning) == "Thinking"
    assert "".join(answers) == "Visible"


def test_reasoning_stream_mux_treats_leading_orphan_close_tag_as_reasoning() -> None:
    answers: list[str] = []
    reasoning: list[str] = []
    mux = ReasoningStreamMux(on_answer=answers.append, on_reasoning=reasoning.append)

    mux.feed("scratch work</think>Visible answer")
    mux.flush()

    assert "".join(reasoning) == "scratch work"
    assert "".join(answers) == "Visible answer"
