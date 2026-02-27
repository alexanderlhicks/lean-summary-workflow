# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using the Google Gemini API. It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Features

*   **Multi-Agent Orchestration:** Employs a pipeline of specialized AI agents (Triage, Summarizer, Synthesizer, Refiner) to ensure high-quality, professional summaries.
*   **High Performance:** Utilizes asynchronous, parallel execution to summarize multiple files simultaneously, drastically reducing the time required for large pull requests.
*   **Smart Triage:** Automatically filters out noise (lockfiles, binaries, generated code) to focus the summary on meaningful changes and save on token costs.
*   **Lean-Aware Analysis:** Tracks `sorry` usages and declaration changes in Lean files, and identifies citations of academic literature or reference materials.
*   **Optional Style Guide Adherence Check:** Automatically reviews code changes against a specified style guide (e.g., `CONTRIBUTING.md`) to ensure consistency.
*   **Customizable AI Prompts:** The behavior and persona of each agent can be easily tailored by modifying external Markdown prompt files.


## How it Works

1.  **Checkout PR Code:** The action begins by checking out the code of the Pull Request, including its full Git history to allow for accurate diff generation.
2.  **Set up Python:** Configures the GitHub Actions environment with Python to run the summary script.
3.  **Install Python Dependencies:** Installs necessary Python libraries defined in `requirements.txt`.
4.  **Generate Diff:** Creates a `pr.diff` file containing the complete changes between the PR's head and base branches.
5.  **Triage Files:** A Triage Agent reviews the list of changed files and filters out noise (e.g., lockfiles, auto-generated code) to save processing time and tokens.
6.  **Parallel Summarization & Style Check:** The script splits the `pr.diff` into individual file diffs. For each relevant file, a Summarizer Agent concurrently generates a concise summary of its changes. If a `style_guide_path` is provided, a Style Checker Agent concurrently reviews the full diff against the guide.
7.  **Analyze Diff for `sorry`s:** The script analyzes the `pr.diff` to identify and categorize `sorry`s that have been added, removed, or affected by line changes.
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
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          github_repository: ${{ github.repository }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional: Path to a style guide file (defaults to 'CONTRIBUTING.md')
          # style_guide_path: 'docs/my-style-guide.md'
          # Optional: Specify a different Gemini model (default: 'gemini-3-flash-preview')
          # gemini_model: "gemini-3-flash-preview"
          # Optional: Comma-separated list of keywords to track for sorrys (default: 'def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom')
          # lean_keywords: 'def,lemma'
```

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | The GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | `true` | |
| `gemini_api_key` | The API key for the Gemini API, used for summary generation. Store this as a secret in your repository. | `true` | |
| `github_repository` | The GitHub repository in the format `owner/repo`. Should be set to `${{ github.repository }}`. | `true` | |
| `pr_number` | The pull request number. Should be set to `${{ github.event.pull_request.number }}`. | `true` | |
| `gemini_model` | The Gemini model to use for the summary. | `false` | `gemini-3-flash-preview` |
| `lean_keywords`| A comma-separated list of keywords to track for `sorry`s in `.lean` files. | `false` | `def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom` |
| `style_guide_path`| Optional: Path to a style guide file within the repository for adherence checking. | `false` | `CONTRIBUTING.md` |

## Customizing AI Prompts

The intelligence and behavior of the AI are primarily governed by Markdown prompt templates stored in the `prompts/` directory within this action.

*   `triage.md`: Instructs the Triage Agent on which files to ignore (e.g., lockfiles).
*   `summarize_file.md`: Contains the instructions for the AI when generating a concise summary for individual files.
*   `check_style.md`: Provides the rules and context for the AI to check code changes against the specified style guide.
*   `synthesize_summary.md`: Guides the AI in generating the draft high-level summary from the per-file summaries.
*   `refine_summary.md`: Instructs the Refiner Agent to review and polish the draft synthesis.

You can modify these `.md` files directly within your forked repository to fine-tune the AI's persona, summarization criteria, style checking rules, or desired output format. Placeholders like `` `{{FILE_LIST}}` ``, `` `{{FILE_PATH}}` ``, `` `{{FILE_DIFF}}` ``, `` `{{PR_TITLE}}` ``, `` `{{PR_BODY}}` ``, `` `{{PER_FILE_SUMMARIES}}` ``, `` `{{STYLE_GUIDE_CONTENT}}` ``, `` `{{DIFF_CONTENT}}` ``, and `` `{{DRAFT_SUMMARY}}` `` are used to inject dynamic information into the prompts during runtime. Ensure these placeholders are kept intact if you wish the AI to receive the corresponding context.

## License

This project is licensed under the [Apache License 2.0](LICENSE).
