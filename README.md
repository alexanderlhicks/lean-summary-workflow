# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using an LLM API (Gemini, Anthropic Claude, or OpenAI GPT). It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Features

*   **Multi-Provider Support:** Supports Gemini, Anthropic (Claude), and OpenAI (GPT) as interchangeable backends via a provider abstraction layer. Each provider handles text and JSON generation with smart retry logic (distinguishes retryable from fatal errors) and rate limiting.
*   **Multi-Agent Pipeline:** Employs a pipeline of specialized AI agents (Triage, Summarizer, Synthesizer, Refiner) to ensure high-quality, professional summaries.
*   **Parallel Execution:** Summarizes multiple files concurrently (up to 10 workers), with per-file diff caching to avoid re-summarizing unchanged files across PR updates.
*   **Smart Triage:** Automatically filters out noise (lockfiles, binaries, generated code) to focus the summary on meaningful changes. Files with proof-relevant signals (`sorry`, `admit`, `native_decide`) are always included regardless of triage decisions.
*   **Lean-Aware Analysis:**
    *   **Source-level declaration lookup:** Loads full source files (new from disk, old via `git show`) to build declaration indices. Sorry/admit occurrences are attributed to their enclosing declaration even when only the proof body changed — not just when the declaration header appears in the diff.
    *   **Nested block comment awareness:** Uses Lean 4's `/- /- ... -/ -/` nested block comment parser to avoid false positives in sorry/quality signal detection.
    *   **Sorry delta:** Top-level summary shows net proof progress (sorries added vs. removed).
    *   **Declaration tracking:** Reports new, removed, and affected declarations.
    *   **Quality signals:** Warns on `admit`, `native_decide`, debug commands (`#check`/`#eval`), and `set_option autoImplicit true` in added lines.
    *   **Issue linking:** Links affected sorries to open GitHub issues labeled `proof wanted`.
*   **Large-PR Scaling:** For PRs with many files (50+), automatically switches to tiered triage (high/low priority) and two-stage synthesis (per-directory then global). Individual file diffs exceeding the per-file size budget are truncated at hunk boundaries, with a coverage note in the output. Style checking is skipped entirely for very large diffs to avoid misleading partial results.
*   **Per-File Summary Caching:** Caches file summaries in a hidden HTML comment on the PR. On subsequent runs (e.g., `synchronize` events), only files whose diffs changed are re-summarized. Cache is invalidated when the model or prompt template changes.
*   **Optional Style Guide Adherence Check:** Reviews code changes against a specified style guide (e.g., `CONTRIBUTING.md`).
*   **Optional PR Title Validation:** Validates PR titles against conventional commit format (`type[(scope)]: subject`) and uses the parsed type to inform summary structure.
*   **Upstream Path Reminders:** Flags when changed files fall under a configurable path prefix (e.g., `ToMathlib/`) and reminds about upstream PRs.
*   **Token Usage Tracking:** Logs cumulative input, output, and thinking token usage across all API calls.

## How it Works

1.  **Checkout & Setup:** Checks out the PR code with full Git history, sets up Python 3.13, and installs dependencies.
2.  **Generate Diff:** Computes the merge base between the PR head and base branches, then generates `pr.diff`. The merge base SHA is exported for source-level lookups.
3.  **Analyze Diff:** The `DiffAnalyzer` parses the full diff to extract statistics, sorry tracking (with source-level declaration attribution), declaration changes, and quality signal warnings. Nested block comments are correctly handled.
4.  **Triage Files:** A Triage Agent reviews the file list and filters out noise. For large PRs (50+ files), files are classified into high/low priority tiers. Files containing proof-relevant signals are always promoted to high priority.
5.  **Parallel Summarization:** Each high-priority file's diff is summarized concurrently by a Summarizer Agent. Cached summaries from previous runs are reused when the diff hash matches. Large individual file diffs are truncated at hunk boundaries.
6.  **Style Check (optional):** If a style guide is available and the diff is within the style-analysis size budget, a Style Checker Agent reviews the changes concurrently with file summarization.
7.  **Synthesis:** The Synthesis Agent generates a structured overview from per-file summaries, PR title, and body. For very large PRs (40+ summaries), uses two-stage synthesis: per-directory groups first, then global.
8.  **Refinement:** A Refiner Agent reviews the draft for accuracy, brevity, and professional tone.
9.  **Post Comment:** The final summary (including sorry delta, statistics, declaration changes, quality signals, coverage notes, style report, and per-file summaries) is posted as a PR comment. Previous summary comments are found and updated.

## Usage

Create a workflow file at `.github/workflows/pr_summary.yml`:

```yaml
name: 'PR Summary'

on:
  pull_request_target:
    types: [opened, synchronize]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write
  issues: read

jobs:
  summarize:
    runs-on: ubuntu-latest
    steps:
      - name: Generate PR Summary
        uses: alexanderlhicks/lean-summary-workflow@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.LLM_API_KEY }}
          provider: gemini  # or: anthropic, openai
          model: gemini-3-flash-preview  # or: claude-sonnet-4-6, gpt-5.4-mini
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional:
          # style_guide_path: 'CONTRIBUTING.md'
          # validate_title: 'true'
          # upstream_path: 'ToMathlib/'
```

> **Note on the trigger:** This example uses `pull_request_target` so the workflow also runs for PRs from forks (the `pull_request` event does not expose repository secrets to fork-triggered workflows, and its `GITHUB_TOKEN` is read-only). `pull_request_target` runs in the context of the base branch, so take care not to execute untrusted code from the fork. This action is safe under `pull_request_target` because it only reads the diff and posts a comment — it does not execute code from the PR branch. The checkout uses `pull_request.head.sha` to fetch the correct diff, while the workflow itself runs from the base branch. If your repository does not accept fork PRs, you can switch the trigger to `pull_request` without other changes.
>
> The `issues: read` permission is used to link affected sorries to open GitHub issues labeled `proof wanted`.

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | Yes | |
| `api_key` | API key for the LLM provider. Store as a repository secret. | Yes | |
| `provider` | LLM provider: `gemini`, `anthropic`, or `openai`. | No | `gemini` |
| `model` | The LLM model to use (e.g., `gemini-3-flash-preview`, `claude-sonnet-4-6`, `gpt-5.4-mini`). | Yes | |
| `github_repository` | The GitHub repository in `owner/repo` format. | Yes | |
| `pr_number` | The pull request number. | Yes | |
| `lean_keywords` | Comma-separated list of Lean declaration keywords to track for sorry attribution. | No | `def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom` |
| `style_guide_path` | Path to a style guide file for adherence checking. | No | `CONTRIBUTING.md` |
| `validate_title` | Validate PR title against conventional commit format: `type[(scope)]: subject`. | No | `false` |
| `upstream_path` | Path prefix for upstream-bound files. If changed files match, a reminder is shown. | No | |

## Project Structure

```
lean-summary-workflow/
  action.yml               # GitHub Actions composite action definition
  summary.py               # Main summary orchestration (multi-agent pipeline)
  llm_provider.py          # LLM provider abstraction (Gemini, Anthropic, OpenAI)
  lean_utils.py            # Lean 4 nested block comment parser
  requirements.txt         # Python dependencies
  prompts/
    triage.md              # Triage agent: file filtering
    triage_tiered.md       # Triage agent: high/low priority classification (50+ files)
    summarize_file.md      # Summarizer agent: per-file summary generation
    check_style.md         # Style checker: diff vs. style guide
    synthesize_summary.md  # Synthesis agent: draft overview from per-file summaries
    refine_summary.md      # Refiner agent: polish draft into final summary
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
| `check_style.md` | Style Checker | `{{STYLE_GUIDE_CONTENT}}`, `{{DIFF_CONTENT}}` |
| `synthesize_summary.md` | Synthesizer | `{{PR_TITLE}}`, `{{PR_BODY}}`, `{{PER_FILE_SUMMARIES}}`, `{{PR_TYPE_HINT}}` |
| `refine_summary.md` | Refiner | `{{PR_TITLE}}`, `{{PR_BODY}}`, `{{DRAFT_SUMMARY}}` |

## Development

### Running Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

### Dependencies

See `requirements.txt`: `PyGithub`, `google-genai`, `anthropic`, `openai`.

### CI

The repository includes a CI workflow (`.github/workflows/ci.yml`) that runs:
- `ruff` linting on `summary.py`
- `action.yml` YAML validation
- Prompt template existence checks
- Env var cross-validation (ensures every env var `summary.py` reads is provided by `action.yml`)
- Dry-run tests (DiffAnalyzer, split_diff, config fingerprint, title validation, sorry delta)

## License

This project is licensed under the [Apache License 2.0](LICENSE).
