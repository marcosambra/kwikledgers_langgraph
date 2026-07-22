from typing import Any


def build_daily_summary_runtime(*args: Any, **kwargs: Any) -> Any:
	from kwikledgers_langgraph.runtime import build_daily_summary_runtime as _build

	return _build(*args, **kwargs)


def build_story_pull_request_runtime(*args: Any, **kwargs: Any) -> Any:
	from kwikledgers_langgraph.runtime import build_story_pull_request_runtime as _build

	return _build(*args, **kwargs)


__all__ = ["build_daily_summary_runtime", "build_story_pull_request_runtime"]