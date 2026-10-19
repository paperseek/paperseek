from __future__ import annotations

import os
from pathlib import Path

from paperseek_app.execution import WorkflowExecution
from paperseek_community.fixed_source import FixedLLMAdapter, FixedSourceAdapter
from paperseek_core.models import QueryLoopBudget, ResearchWorkflowRequest

import paperseek_community.cli as community_cli


class _ControlledExecutionResolver:
    def __init__(self, _settings, _credentials, **_options) -> None:
        self.fixture = Path(os.environ["PAPERSEEK_CLI_FIXTURE"])

    def __call__(self, run, _cancellation) -> WorkflowExecution:
        source = FixedSourceAdapter.from_file(self.fixture)
        return WorkflowExecution(
            llm=FixedLLMAdapter(source.fixture.llm_responses),
            source=source,
            request=ResearchWorkflowRequest(
                question=run.question,
                query_budget=QueryLoopBudget(
                    target_min=1,
                    target_max=50,
                    retrieval_pool_min=1,
                    query_portfolio_enabled=False,
                ),
            ),
        )


community_cli.CommunityExecutionResolver = _ControlledExecutionResolver
