# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using an LLM API (Gemini, Anthropic Claude, or OpenAI GPT). It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Features

*   **Multi-Agent Orchestration:** Employs a pipeline of specialized AI agents (Triage, Summarizer, Synthesizer, Refiner) to ensure high-quality, professional summaries.
*   **High Performance:** Utilizes asynchronous, parallel execution to summarize multiple files simultaneously, drastically reducing the time required for large pull requests.
*   **Smart Triage:** Automatically filters out noise (lockfiles, binaries, generated code) to focus the summary on meaningful changes and save on token costs.
*   **Lean-Aware Analysis:** Tracks `sorry` usages and declaration changes in Lean files. Displays a top-level sorry delta showing net proof progress. Warns on `admit`, `native_decide`, debug commands (`#check`/`#eval`), and `set_option autoImplicit true`.
*   **Large-PR Scaling:** For PRs with many files, automatically switches to tiered triage (high/low priority) and two-stage synthesis (per-directory then global) to stay within model context limits.
*   **Optional Style Guide Adherence Check:** Automatically reviews code changes against a specified style guide (e.g., `CONTRIBUTING.md`) to ensure consistency.
*   **Optional PR Title Validation:** Validates PR titles against conventional commit format (`type[(scope)]: subject`) and uses the parsed type to inform summary structure.
*   **Upstream Path Reminders:** Flags when changed files fall under a configurable path prefix (e.g., `ToMathlib/`) and reminds about upstream PRs.
*   **Customizable AI Prompts:** The behavior and persona of each agent can be easily tailored by modifying external Markdown prompt files.


## How it Works

1.  **Checkout PR Code:** The action begins by checking out the code of the Pull Request, including its full Git history to allow for accurate diff generation.
2.  **Set up Python:** Configures the GitHub Actions environment with Python to run the summary script.
3.  **Install Python Dependencies:** Installs necessary Python libraries defined in `requirements.txt`.
4.  **Generate Diff:** Creates a `pr.diff` file containing the complete changes between the PR's head and base branches.
5.  **Triage Files:** A Triage Agent reviews the list of changed files and filters out noise (e.g., lockfiles, auto-generated code) to save processing time and tokens. For large PRs (50+ files), the agent assigns priority tiers; files with proof-relevant signals (`sorry`, `admit`, `native_decide`) are always high priority.
6.  **Parallel Summarization & Style Check:** The script splits the `pr.diff` into individual file diffs. For each relevant file, a Summarizer Agent concurrently generates a concise summary of its changes. If a `style_guide_path` is provided, a Style Checker Agent concurrently reviews the full diff against the guide.
7.  **Analyze Diff for `sorry`s and Quality Signals:** The script analyzes the `pr.diff` to identify and categorize `sorry`s that have been added, removed, or affected by line changes. It also detects `admit`, `native_decide`, debug commands, and `autoImplicit` re-enablement in added Lean lines.
8.  **Synthesize Overall Summary:** The Synthesis Agent takes the individual file summaries, along with the PR title and body, to generate a comprehensive draft overview.
9.  **Refine Summary:** A Refiner Agent reviews the draft synthesis to ensure accuracy, brevity, and professional tone, producing the final PR summary.
10. **Post PR Comment:** The final structured summary, including change statistics, `sorry` tracking, style adherence report (if applicable), and per-file summaries, is posted as a comment on the Pull Request. If a previous summary comment exists, it will be updated.
11. **Clean up:** The temporary `pr.diff` file is removed.

## Usage

To use this action, create a new workflow file in your repository at `.github/workflows/pr_summary.yml`:

```yaml
name: 'PR Summary'

on:
  pull_request:
    types: [opened, synchronize]

permissions:
  contents: read
  pull-requests: write

jobs:
  summarize:
    runs-on: ubuntu-latest
    steps:
      - name: Generate PR Summary
        uses: your-org/your-repo-name@main # Replace with your action's repository
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.LLM_API_KEY }}
          provider: gemini  # or: anthropic, openai
          model: gemini-3-flash-preview  # or: claude-sonnet-4-20250514, gpt-4o-mini
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional: Path to a style guide file (defaults to 'CONTRIBUTING.md')
          # style_guide_path: 'docs/my-style-guide.md'
```

> **Note on forked PRs:** The `pull_request` event does not expose repository secrets to workflows triggered by forks, and the `GITHUB_TOKEN` it provides is read-only. This means the above workflow will fail for PRs from external contributors. If your repository accepts fork PRs, use `pull_request_target` instead — but be aware that `pull_request_target` runs in the context of the base branch, so you must take care not to execute untrusted code from the fork.

<details><summary>Example workflow for public repositories accepting fork PRs</summary>

```yaml
name: 'PR Summary'

on:
  pull_request_target:
    types: [opened, synchronize]

permissions:
  contents: read
  pull-requests: write

jobs:
  summarize:
    runs-on: ubuntu-latest
    steps:
      - name: Generate PR Summary
        uses: your-org/your-repo-name@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.LLM_API_KEY }}
          provider: gemini
          model: gemini-3-flash-preview
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
```

This is safe for this action because it only reads the diff and posts a comment — it does not execute any code from the PR branch. The checkout uses `pull_request.head.sha` to fetch the correct diff, while the workflow itself runs from the base branch.

</details>

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | The GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | `true` | |
| `api_key` | API key for the LLM provider. Store this as a secret in your repository. | `true` | |
| `provider` | LLM provider: `gemini`, `anthropic`, or `openai`. | `false` | `gemini` |
| `github_repository` | The GitHub repository in the format `owner/repo`. Should be set to `${{ github.repository }}`. | `true` | |
| `pr_number` | The pull request number. Should be set to `${{ github.event.pull_request.number }}`. | `true` | |
| `model` | The LLM model to use (e.g., `gemini-3-flash-preview`, `claude-sonnet-4-20250514`, `gpt-4o-mini`). | `true` | |
| `lean_keywords`| A comma-separated list of keywords to track for `sorry`s in `.lean` files. | `false` | `def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom` |
| `style_guide_path`| Optional: Path to a style guide file within the repository for adherence checking. | `false` | `CONTRIBUTING.md` |
| `validate_title` | Validate PR title against conventional commit format: `type[(scope)]: subject`. | `false` | `false` |
| `upstream_path` | Path prefix for upstream-bound files. If changed files match, a reminder is shown. | `false` | |

## Customizing AI Prompts

The intelligence and behavior of the AI are primarily governed by Markdown prompt templates stored in the `prompts/` directory within this action.

*   `triage.md`: Instructs the Triage Agent on which files to ignore (e.g., lockfiles).
*   `triage_tiered.md`: Used for large PRs (50+ files). Classifies files into high/low priority tiers with conservative defaults.
*   `summarize_file.md`: Contains the instructions for the AI when generating a concise summary for individual files.
*   `check_style.md`: Provides the rules and context for the AI to check code changes against the specified style guide.
*   `synthesize_summary.md`: Guides the AI in generating the draft high-level summary from the per-file summaries.
*   `refine_summary.md`: Instructs the Refiner Agent to review and polish the draft synthesis.

You can modify these `.md` files directly within your forked repository to fine-tune the AI's persona, summarization criteria, style checking rules, or desired output format. Placeholders like `` `{{FILE_LIST}}` ``, `` `{{FILE_PATH}}` ``, `` `{{FILE_DIFF}}` ``, `` `{{PR_TITLE}}` ``, `` `{{PR_BODY}}` ``, `` `{{PER_FILE_SUMMARIES}}` ``, `` `{{STYLE_GUIDE_CONTENT}}` ``, `` `{{DIFF_CONTENT}}` ``, and `` `{{DRAFT_SUMMARY}}` `` are used to inject dynamic information into the prompts during runtime. Ensure these placeholders are kept intact if you wish the AI to receive the corresponding context.

## License

This project is licensed under the [Apache License 2.0](LICENSE).
