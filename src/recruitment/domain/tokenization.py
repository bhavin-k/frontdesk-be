"""Local tokenizer shared by RAG and full-text scoring."""

import json
from functools import lru_cache
from importlib.resources import files

import tiktoken
from tiktoken.load import load_tiktoken_bpe


@lru_cache(maxsize=1)
def tokenizer() -> tiktoken.Encoding:
    asset = files("recruitment").joinpath("assets")
    config = json.loads(asset.joinpath("tokenizer.json").read_text(encoding="utf-8"))
    return tiktoken.Encoding(
        name="recruitment_cl100k",
        pat_str=config["pattern"],
        mergeable_ranks=load_tiktoken_bpe(str(asset.joinpath("cl100k_base.tiktoken"))),
        special_tokens=config["special_tokens"],
    )


def token_count(text: str) -> int:
    return len(tokenizer().encode(text, disallowed_special=()))
