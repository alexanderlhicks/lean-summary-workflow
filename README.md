# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using an LLM, accessed through [OpenRouter](https://openrouter.ai). It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Features

*   **Any model via OpenRouter:** A single OpenRouter-backed client reaches Claude, Gemini, GPT, and others — the model is selected purely by its OpenRouter slug (e.g. `anthropic/claude-opus-4.8`), with no per-provider code. Structured (schema-validated) output, multimodal input, reasoning effort, and rate-limit/retry backoff are handled uniformly.
*   **Multi-Agent Pipeline:** Employs a pipeline of specialized AI agents (Triage, Summarizer, Synthesizer) that produce a reviewer-oriented overview. The summary is intended as an entry point: it describes the PR's scope, structure, and contents so a reviewer can orient before opening the diff (deep, suggestion-level review is a separate concern). Prompts favor breadth and a self-contained overview over terseness.
*   **Parallel Execution:** Summarizes multiple files concurrently (up to 10 workers), with per-file diff caching to avoid re-summarizing unchanged files across PR updates.
*   **Smart Triage:** Automatically filters out noise (lockfiles, binaries, generated code) to focus the summary on meaningful changes. Files with proof-relevant signals (`sorry`, `admit`, `native_decide`) are always included regardless of triage decisions.
*   **Lean-Aware Analysis:**
    *   **Source-level declaration lookup:** Loads full source files (new from disk, old via `git show`) to build declaration indices. Sorry/admit occurrences are attributed to their enclosing declaration even when only the proof body changed — not just when the declaration header appears in the diff.
    *   **Nested block comment awareness:** Uses Lean 4's `/- /- ... -/ -/` nested block comment parser to avoid false positives in sorry/quality signal detection.
    *   **Sorry delta:** Top-level summary shows net proof progress (sorries added vs. removed).
    *   **Declaration tracking:** Reports new, removed, and affected declarations.
    *   **Quality signals:** Warns on `admit`, `native_decide`, debug commands (`#check`/`#eval`), and `set_option autoImplicit true` in added lines.
    *   **Issue linking:** Links affected sorries to open GitHub issues labeled `proof wanted`.
*   **Large-PR Scaling:** For PRs with many files (50+), automatically switches to tiered triage (high/low priority) and two-stage synthesis (per-directory then global). Individual file diffs exceeding the per-file size budget are truncated at a hunk boundary where possible (otherwise at a line boundary), with a coverage note in the output. The additional-instructions analysis is skipped entirely when the overall diff exceeds its size budget, to avoid misleading partial results.
*   **Per-File Summary Caching:** Caches file summaries in a hidden HTML comment on the PR. On subsequent runs (e.g., `synchronize` events), only files whose diffs changed are re-summarized. Cache is invalidated when the model or prompt template changes, and is pruned each run to the files in the current diff so it cannot accumulate stale entries (e.g. from renamed/removed files) and bloat the comment.
*   **Optional Additional-Instructions Analysis:** Applies deployment-supplied instructions (via `additional_instructions_path`) to the diff — e.g. a style guide such as `CONTRIBUTING.md`, a progress tracker, or any project-specific guidance. The instructions themselves tell the agent what to produce.
*   **Optional PR Title Validation:** Validates PR titles against conventional commit format (`type[(scope)]: subject`) and uses the parsed type to inform summary structure.
*   **Upstream Path Reminders:** Flags when changed files fall under a configurable path prefix (e.g., `ToMathlib/`) and reminds about upstream PRs.
*   **Token Usage Tracking:** Logs cumulative input, output, and thinking token usage across all API calls.

## How it Works

1.  **Checkout & Setup:** Checks out the PR code with full Git history, sets up Python 3.13, and installs dependencies.
2.  **Generate Diff:** Computes the merge base between the PR head and base branches, then generates `pr.diff`. The merge base SHA is exported for source-level lookups.
3.  **Analyze Diff:** The `DiffAnalyzer` parses the full diff to extract statistics, sorry tracking (with source-level declaration attribution), declaration changes, and quality signal warnings. Nested block comments are correctly handled.
4.  **Triage Files:** A Triage Agent reviews the file list and filters out noise. For large PRs (50+ files), files are classified into high/low priority tiers. Files containing proof-relevant signals are always promoted to high priority.
5.  **Parallel Summarization:** Each high-priority file's diff is summarized concurrently by a Summarizer Agent. Cached summaries from previous runs are reused when the diff hash matches. Large individual file diffs are truncated at hunk boundaries.
6.  **Additional-Instructions Analysis (optional):** If an instructions file is available and the diff is within the analysis size budget, an Additional-Instructions Agent applies those instructions (e.g. a style guide) to the diff concurrently with file summarization.
7.  **Synthesis:** The Synthesis Agent generates a structured, self-contained overview from per-file summaries, PR title, and body. For very large PRs (40+ summaries), uses two-stage synthesis: per-directory groups first, then global. Files triaged out entirely are still noted, so the file count reconciles and nothing is invisible.
8.  **Post Comment:** The final summary (including sorry delta, statistics, declaration changes, quality signals, coverage notes, additional analysis, and per-file summaries) is posted as a PR comment. Declaration and `sorry` listings are grouped by file, sorted deterministically, and capped with an overflow note on very large PRs. The comment body is kept under GitHub's size limit by shedding regenerable content (cache, then per-file summaries) if needed. Previous summary comments are found and updated.

## Deployment

### 1. Add your OpenRouter API key

The action reaches every model through OpenRouter's OpenAI-compatible endpoint (`https://openrouter.ai/api/v1`), selected purely by model slug — there is no per-provider setup.

1. Create an API key at <https://openrouter.ai/keys>.
2. Add it as a repository (or organization) **Actions secret**. These docs use the name **`OPENROUTER_KEY`**; if you pick another name, update the `api_key` line in the workflow below.

### 2. Add the workflow

Create `.github/workflows/summary.yml`:

```yaml
name: 'PR Summary'

on:
  pull_request_target:
    types: [opened, synchronize]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read        # required: the action checks out the PR head to read the diff
  pull-requests: write  # required: post/update the summary comment
  issues: read          # optional: link affected sorries to `proof wanted` issues

jobs:
  summarize:
    runs-on: ubuntu-latest
    steps:
      - name: Generate PR Summary
        uses: alexanderlhicks/lean-summary-workflow@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.OPENROUTER_KEY }}
          model: google/gemini-3-flash-preview   # any OpenRouter slug — see the table below
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional:
          # additional_instructions_path: 'CONTRIBUTING.md'   # style guide / progress tracker / cross-check
          # validate_title: 'true'                            # enforce conventional-commit titles
          # upstream_path: 'ToMathlib/'                       # remind about upstream-bound files
          # reasoning_effort: 'low'                           # low|medium|high; default off (ignored by non-reasoning models)
          # max_file_diff_chars: '60000'                      # per-file diff budget (~1,200 Lean lines)
          # max_instructions_diff_chars: '400000'             # whole-PR budget for the instructions agent (~100k tokens)
```

> **Why `pull_request_target`?** It lets the workflow run for fork PRs *with* access to secrets (the `pull_request` event gives fork-triggered runs a read-only token and no secrets). The job runs from your **default branch**, not the PR branch — so this is also why a workflow change only takes effect once merged to the default branch. The action is safe here because it only *reads* the diff (checked out at `pull_request.head.sha`) and posts a comment; it never executes code from the PR branch. If you don't accept fork PRs, you can switch the trigger to `pull_request` with no other changes.

### 3. Choose a model

Any OpenRouter slug works. The pipeline is light (triage, short per-file summaries, one synthesis), so a **cheap instruction-following model is the sweet spot**. Verified candidates (prices per 1M tokens, input/output):

| Slug | Context | In / Out | Notes |
|---|---|---|---|
| `google/gemma-4-26b-a4b-it` | 256k | $0.06 / $0.33 | Cheapest capable; great default for cost. |
| `openai/gpt-5-nano` | 400k | $0.05 / $0.40 | Cheapest overall, large context, reliable JSON. |
| `qwen/qwen3-235b-a22b-2507` | 256k | $0.09 / $0.10 | Very low output cost; strong. |
| `mistralai/mistral-small-3.2-24b-instruct` | 128k | $0.08 / $0.20 | Cheap and solid. |
| `deepseek/deepseek-chat-v3.1` | 164k | $0.21 / $0.79 | Strong, mid-cheap. |
| `google/gemini-3.1-flash-lite` | 1M | $0.25 / $1.50 | Huge context, mid-cheap. |
| `openai/gpt-5-mini` | 400k | $0.25 / $2.00 | Very reliable structured output. |
| `google/gemini-3-flash-preview` | 1M | $0.50 / $3.00 | Capable, large context. |
| `anthropic/claude-haiku-4.5` | 200k | $1.00 / $5.00 | Most reliable structured output; priciest of the cheap tier. |

Guidance:
- **Lowest cost:** `google/gemma-4-26b-a4b-it`, `openai/gpt-5-nano`, or `qwen/qwen3-235b-a22b-2507`.
- **Most reliable structured output** (triage and per-file summaries use schema-validated JSON): `anthropic/claude-haiku-4.5`, `openai/gpt-5-mini`, `google/gemini-3-flash-preview`. The action enables OpenRouter's response-healing for malformed JSON, but if a very cheap model's triage step misbehaves, move up a tier.
- **Reasoning effort** (`reasoning_effort`) is honored by reasoning-capable models (GPT-5, Gemini 3) and silently ignored by others (Gemma, Qwen, Mistral).
- Run `curl -s https://openrouter.ai/api/v1/models` for the current catalogue and live pricing.

### 4. Size the diff budgets to your model (optional)

`max_file_diff_chars` and `max_instructions_diff_chars` bound how much diff is sent to the summarizer and the additional-instructions agent. The defaults (60k / 400k chars ≈ 15k / 100k tokens) fit every model in the table above (all ≥128k context). Lower them for a smaller-context model; raise `max_file_diff_chars` to truncate large files less, at higher token cost. See [Inputs](#inputs) for the line↔char rule of thumb.

### 5. Verify it works

Open or push to a PR. The action posts (and thereafter updates) a single **🤖 PR Summary** comment. Check the run under the repository's **Actions** tab — the logs end with a cumulative token-usage line. The comment is keyed by a hidden marker, so subsequent commits update the same comment rather than adding new ones.

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | Yes | |
| `api_key` | OpenRouter API key. Store as a repository secret. | Yes | |
| `model` | OpenRouter model slug (e.g., `anthropic/claude-opus-4.8`, `google/gemini-3-pro-preview`, `openai/gpt-5`). | Yes | |
| `github_repository` | The GitHub repository in `owner/repo` format. | Yes | |
| `pr_number` | The pull request number. | Yes | |
| `lean_keywords` | Comma-separated list of Lean declaration keywords to track for sorry attribution. | No | `def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom` |
| `additional_instructions_path` | Path to a file with deployment-supplied instructions for the analysis agent. Use it for style guides, progress trackers, framework cross-checks, doc/wiki references, or any project-specific guidance the LLM should apply to the PR diff. The instructions themselves tell the agent what to produce. | No | `CONTRIBUTING.md` |
| `reasoning_effort` | Reasoning/thinking effort applied to every model call: `low`, `medium`, or `high`. Empty uses the model default. Ignored by models without reasoning support. | No | `` |
| `validate_title` | Validate PR title against conventional commit format: `type[(scope)]: subject`. | No | `false` |
| `upstream_path` | Path prefix for upstream-bound files. If changed files match, a reminder is shown. | No | |
| `max_file_diff_chars` | Max characters of a single file's diff sent to the summarizer before it is truncated at a hunk boundary. Rough guide: Lean averages ~50 chars/line (~4 chars/token), so `60000` ≈ ~1,200 lines ≈ ~15k tokens. Lower for a smaller-context model. | No | `60000` |
| `max_instructions_diff_chars` | Max characters of the whole-PR diff sent to the additional-instructions agent in one call; above this the analysis is skipped. Must fit the model's context alongside the instructions file and response. Rough guide: `400000` ≈ ~8,000 changed lines ≈ ~100k tokens (fits a ~128k-token model). Lower for a smaller-context model. | No | `400000` |

## Project Structure

```
lean-summary-workflow/
  action.yml               # GitHub Actions composite action definition
  summary.py               # Main summary orchestration (multi-agent pipeline)
  llm_provider.py          # OpenRouter-backed LLM client (single provider)
  lean_utils.py            # Lean 4 nested block comment parser
  pyproject.toml           # Project metadata + dependencies (uv)
  uv.lock                  # Pinned dependency lockfile
  prompts/
    triage.md              # Triage agent: file filtering
    triage_tiered.md       # Triage agent: high/low priority classification (50+ files)
    summarize_file.md          # Summarizer agent: per-file summary generation
    additional_instructions.md # Additional-instructions agent: applies deployment-supplied instructions to the diff
    synthesize_summary.md      # Synthesis agent: self-contained overview from per-file summaries
  tests/
    test_summary.py        # Unit tests
```

## Customizing AI Prompts

The behavior of each AI agent is governed by Markdown prompt templates in the `prompts/` directory. Each template uses `{{PLACEHOLDER}}` syntax for dynamic content injection at runtime.

**Prompt files and their placeholders:**

| Prompt | Agent | Placeholders |
|--------|-------|-------------|
| `triage.md` | Triage (normal) | `{{FILE_LIST}}` |
| `triage_tiered.md` | Triage (50+ files) | `{{FILE_LIST}}` |
| `summarize_file.md` | Summarizer | `{{FILE_PATH}}`, `{{FILE_DIFF}}` |
| `additional_instructions.md` | Additional-instructions | `{{INSTRUCTIONS_CONTENT}}`, `{{DIFF_CONTENT}}` |
| `synthesize_summary.md` | Synthesizer | `{{PR_TITLE}}`, `{{PR_BODY}}`, `{{PER_FILE_SUMMARIES}}`, `{{PR_TYPE_HINT}}` |

## Development

### Running Tests

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest tests/ -v   # uv resolves the env from uv.lock automatically
uv run ruff check summary.py
```

### Dependencies

Declared in `pyproject.toml` (pinned in `uv.lock`): `PyGithub`, `openai` (pointed at OpenRouter), `pydantic`.

### CI

The repository includes a CI workflow (`.github/workflows/ci.yml`) that runs:
- `ruff` linting on `summary.py`
- `action.yml` YAML validation
- Prompt template existence checks
- Env var cross-validation (ensures every env var `summary.py` reads is provided by `action.yml`)
- Dry-run tests (DiffAnalyzer, split_diff, config fingerprint, title validation, sorry delta)

## License

This project is licensed under the [Apache License 2.0](LICENSE).
