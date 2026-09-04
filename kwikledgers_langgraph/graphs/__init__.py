from typing import Any


def build_daily_summary_graph(*args: Any, **kwargs: Any) -> Any:
	from kwikledgers_langgraph.graphs.daily_summary import build_daily_summary_graph as _build

	return _build(*args, **kwargs)


def build_story_pull_request_graph(*args: Any, **kwargs: Any) -> Any:
	from kwikledgers_langgraph.graphs.story_pull_request import build_story_pull_request_graph as _build

	return _build(*args, **kwargs)


__all__ = ["build_daily_summary_graph", "build_story_pull_request_graph"]