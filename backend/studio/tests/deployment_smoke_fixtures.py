"""Deterministic response fixture for the shared normalization parser tests."""


def test_normalization_response(payload, text="A restrained slow camera push-in."):
    from studio.services.prompt_normalization_contract import _protected_token_sentinel
    return {"schema_version": 1, "items": [
        {"request_id": item["request_id"], "translation": text + " " + " ".join(
            _protected_token_sentinel(token)
            for token in item.get("protected_tokens", [])
            for _ in range(item["text"].count(token))
        )} for item in payload["items"]], "metadata": {}}
