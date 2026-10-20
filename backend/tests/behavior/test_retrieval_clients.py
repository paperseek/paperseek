from __future__ import annotations

import asyncio
import unittest

import httpx
from fixtures.retrieval_client import FakeClient

from paperseek_app.execution import CancellationScope
from paperseek_core.models import PaperRecord
from paperseek_core.retrieval_clients import (
    ExternalEmbeddingAdapter,
    ExternalRerankerAdapter,
)


class BlockingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def post(self, url: str, *, json: dict, headers: dict) -> httpx.Response:
        del url, json, headers
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def record(uid: str, title: str) -> PaperRecord:
    return PaperRecord(uid=uid, title=title)


class M4RetrievalServiceContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_batches_restore_global_input_order(self) -> None:
        def response(size):
            return (200, {"data": [
                {"index": index, "embedding": [1.0, float(index % 2)]}
                for index in reversed(range(size))
            ]})

        client = FakeClient([response(64), response(64), response(3)])
        adapter = ExternalEmbeddingAdapter(
            client, CancellationScope(), provider="cstcloud", model="fixture",
            base_url="https://embedding.example/v1", api_key="fixture",
        )
        documents = [record(str(index), f"Paper {index}") for index in range(130)]
        result = await adapter.embedding_scores("question", documents)
        scores = result.scores
        self.assertEqual([len(call["json"]["input"]) for call in client.calls], [64, 64, 3])
        self.assertEqual(
            [text for call in client.calls for text in call["json"]["input"]],
            ["question", *[document.title for document in documents]],
        )
        self.assertEqual(len(scores), 130)
        self.assertAlmostEqual(scores[0], 2 ** -0.5)
        self.assertEqual(scores[63], 1.0)

    async def test_openai_compatible_embedding_scores_and_model_fallback(self) -> None:
        client = FakeClient([
            (400, {"error": "synthetic model rejection"}),
            (200, {"data": [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 1, "embedding": [1.0, 0.0]},
                {"index": 2, "embedding": [0.0, 1.0]},
            ]}),
        ])
        adapter = ExternalEmbeddingAdapter(
            client, CancellationScope(), provider="cstcloud",
            model="first-model,second-model", base_url="https://embedding.example/v1", api_key="fixture-key",
        )

        scores = await adapter.embedding_scores("graph", [record("a", "graph"), record("b", "policy")])

        self.assertEqual(scores.scores, [1.0, 0.0])
        self.assertEqual(scores.model, "second-model")
        self.assertEqual([call["json"]["model"] for call in client.calls], ["first-model", "second-model"])
        self.assertEqual(client.calls[-1]["url"], "https://embedding.example/v1/embeddings")
        self.assertEqual(client.calls[-1]["json"]["encoding_format"], "float")
        self.assertEqual(client.calls[-1]["headers"]["Authorization"], "Bearer fixture-key")



    async def test_nvidia_reranker_maps_indexed_scores(self) -> None:
        client = FakeClient([(200, {"rankings": [
            {"index": 1, "logit": 0.95},
            {"index": 0, "logit": 0.2},
        ]})])
        adapter = ExternalRerankerAdapter(
            client, CancellationScope(), provider="nvidia",
            model="nv-rerank-qa-mistral-4b:1",
            base_url="https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
            api_key="fixture-key",
        )
        documents = [record("a", "First"), record("b", "Second")]

        result = await adapter.rerank("question", documents)

        self.assertEqual([(item.document.uid, item.score) for item in result.items], [("b", 0.95), ("a", 0.2)])
        self.assertEqual(client.calls[0]["json"]["query"], {"text": "question"})
        self.assertEqual(client.calls[0]["json"]["passages"], [{"text": "First"}, {"text": "Second"}])


if __name__ == "__main__":
    unittest.main()
