"""Offline API fixture for the opt-in review browser acceptance flow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import uvicorn

sys.path.insert(0, str(Path(__file__).parents[1] / "behavior"))
from fixtures.review_support import ROUTE, ReviewLLM, create_source, review_settings  # noqa: E402
from paperseek_app.review.execution import ReviewExecution  # noqa: E402
from paperseek_community.api import create_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--static", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8778)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    run = create_source(args.directory)
    llm = ReviewLLM()

    def reject_search(*_):
        raise AssertionError("This offline fixture only runs review stages")

    app = create_app(args.directory / "paperseek.db", static_dir=args.static,
                     deployment_settings=review_settings(), execution_resolver=reject_search,
                     review_execution_resolver=lambda *_: ReviewExecution(llm=llm, route=ROUTE))
    (args.directory / "fixture.json").write_text(json.dumps({"source_run_id": run.id}), encoding="utf-8")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
