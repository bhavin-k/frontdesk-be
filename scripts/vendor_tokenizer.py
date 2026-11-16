"""Vendor the pinned tokenizer vocabulary so workers and tests need no tokenizer network access."""

import hashlib
import json
import urllib.request
from pathlib import Path

import tiktoken

URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
EXPECTED = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "src/recruitment/assets"
    destination.mkdir(parents=True, exist_ok=True)
    content = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(content).hexdigest() != EXPECTED:
        raise RuntimeError("Tokenizer checksum mismatch.")
    (destination / "cl100k_base.tiktoken").write_bytes(content)
    encoding = tiktoken.get_encoding("cl100k_base")
    (destination / "tokenizer.json").write_text(
        json.dumps(
            {
                "pattern": encoding._pat_str,
                "special_tokens": encoding._special_tokens,
                "sha256": EXPECTED,
                "source": URL,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Pinned tokenizer vocabulary verified and vendored.")


if __name__ == "__main__":
    main()
