# Known log noise

Log and warning output that looks alarming but is expected. Before debugging a
scary-looking line, check here. Add new entries as they are diagnosed; remove
entries when the underlying cause goes away.

## `AFC is enabled with max remote calls: 10` / "Direct use of automatic function calling … is not recommended"

Logged by `google_genai.models` on every Gemini structured-output call (INFO +
WARNING pair). Structured output via LangChain's `with_structured_output` is
function calling under the hood, which trips the Google SDK's
automatic-function-calling (AFC) heuristic. Both lines are false positives:
LangChain passes tool *schemas*, not executable callables, so AFC never
actually runs — there is nothing for the SDK to auto-execute, and LangChain
parses the returned function call itself.

Do **not** follow the warning's advice to switch to the Google SDK's `Chat`
API: loop control belongs in LangGraph, where it stays provider-agnostic and
inside our error mapping and cost accounting (`docs/ARCHITECTURE.md`, "LLM
layer"). If the noise bothers you, raise the `google_genai.models` logger
level to ERROR.

## `generate-resume cache create skipped status=400`

Logged by `app.generate.llm` when explicit prompt-cache creation is rejected —
typically because the shared prefix is below the model's minimum cacheable
token count, or the model doesn't support explicit caching. This is a handled
fallback, not a failure: generation proceeds normally and still benefits from
implicit caching. The 400 is remembered per cache key so it isn't retried.

## `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` (pytest)

Emitted once per test session from `fastapi.testclient`. Upstream
FastAPI/Starlette migration notice (`httpx` → `httpx2`); harmless until we
bump those dependencies together. Not worth suppressing — it will disappear
with the dependency upgrade.
