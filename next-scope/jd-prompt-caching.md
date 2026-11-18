# Cache the JD across CV matches

Proposed for a future backend change. When an opening receives many CVs, reuse the scoring instructions, result schema and saved job description (JD). Keep the original CV profile JSON and one independent score per CV and opening.

## Implementation

1. Add a native Claude Messages adapter for the matcher and select it through the existing environment-based configuration. The current Claude Chat Completions compatibility endpoint lacks prompt caching. [Claude API compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)
2. Send the scoring instructions and result schema, then the saved JD as compact JSON, then the masked CV profile. Mark the JD block with `cache_control` so the cache ends before the CV profile. Reuse the exact same prefix for CVs with the same opening snapshot.
3. Use each CV’s arrival-time JD snapshot. An edit creates a new prefix for future arrivals. Validate each match result with the existing schema and experience check before storing its score.
4. Start with the five-minute cache and pilot one busy opening. Haiku 5.5 needs at least 512 tokens in the shared prefix. Each hit refreshes the cache lifetime. Consider the one-hour option if the gap between matches makes the five-minute cache miss. [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## Verify the pilot

- Use fictional CVs against one JD. Check for a cache write on the first match and cache reads on subsequent matches. Log `cache_creation_input_tokens` and `cache_read_input_tokens` alongside uncached input and output counts.
- Use fake providers in automated tests. Check that several CVs each get an independent score, a JD edit applies to future arrivals, and a cache miss still runs the standard matching flow.

## Cost example

For 50 matches with a 1,000-token shared prefix, the regular input charge covers 50,000 tokens. With one five-minute cache write at 1.25 times the standard input price and 49 hits at one-tenth of that price, the prefix costs the equivalent of 6,150 regular input tokens. That is about 88% less for the repeated block. The CV input and generated score for each request carry their own charges. [Claude caching prices](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pricing)

## Other options

- Use Claude Message Batches if recruiters can wait for asynchronous results. Submit one request per CV at half the standard rate. [Claude batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- Use a provider cache reference if Frontdesk moves to a supported Gemini endpoint. Submit the cached JD reference and the new CV profile with each request. [Gemini context caching](https://aistudio.google.com/docs/caching?generate-content=true)
