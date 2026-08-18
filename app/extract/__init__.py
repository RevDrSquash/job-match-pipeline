"""extract-job: structured LLM extraction, skill linking, synth doc, embedding.

No package-level re-exports: ``embed`` and ``service`` import ``app.skills``,
which imports ``app.extract.llm`` back — eager imports here made any cold
``import app.skills`` fail with a circular-import error. Import the submodule
you need (``app.extract.llm``, ``app.extract.embed``, ...) directly.
"""
