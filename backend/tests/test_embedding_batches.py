from types import SimpleNamespace

from app.core.config import settings
from app.services.llm_service import EmbeddingClient


def test_all_long_video_chunks_keep_their_order_across_provider_batches(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-key")
    batches = []
    class Provider:
        embeddings = None
        def __init__(self):
            self.embeddings = self
        def create(self, *, model, input):
            assert len(input) <= 10
            batches.append(list(input))
            return SimpleNamespace(data=[
                SimpleNamespace(index=i, embedding=[float(value)])
                for i, value in reversed(list(enumerate(input)))
            ])
    service = EmbeddingClient()
    service._client = Provider()
    source = [str(i) for i in range(23)]
    result = EmbeddingClient.embed_texts.__wrapped__(service, source)
    assert [value[0] for value in result] == list(range(23))
    assert [chunk for batch in batches for chunk in batch] == source
