# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using the Google Gemini API. It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects.

For pull requests with multiple file changes, the action employs a hierarchical approach: it first generates a summary for each individual file's changes and then synthesizes these into a comprehensive, high-level overview of the entire pull request. This ensures that even complex changes are accurately and clearly summarized.

## Features

*   **AI-Powered PR Summarization:** Generates a concise, high-level summary of pull request changes. For PRs affecting multiple files, it summarizes each file individually and then synthesizes these into an overall summary.
*   **Lean `sorry` Tracking:** Identifies and tracks `sorry` usages in Lean files, helping to manage formalization completeness.
*   **Optional Style Guide Adherence Check:** Can automatically review code changes against a specified style guide (e.g., `CONTRIBUTING.md`) to ensure consistency.
*   **Customizable AI Prompts:** The AI's behavior and output can be easily tailored by modifying external Markdown prompt files.


## How it Works

1.  **Checkout PR Code:** The action begins by checking out the code of the Pull Request, including its full Git history to allow for accurate diff generation.
2.  **Set up Python:** Configures the GitHub Actions environment with Python to run the summary script.
3.  **Install Python Dependencies:** Installs necessary Python libraries defined in `requirements.txt`.
4.  **Generate Diff:** Creates a `pr.diff` file containing the complete changes between the PR's head and base branches.
5.  **Summarize Per-File Changes:** The `summary.py` script splits the `pr.diff` into individual file diffs. For each `.lean` file, it calls the Gemini API to generate a concise, one-sentence summary of its changes.
6.  **Perform Style Guide Check (Optional):** If a `style_guide_path` is provided, the script reads the content of that file and sends it along with the full PR diff to the Gemini API. The AI then reviews the changes against the style guide and reports any deviations.
7.  **Analyze Diff for `sorry`s:** The script analyzes the `pr.diff` to identify and categorize `sorry`s that have been added, removed, or affected by line changes.
8.  **Synthesize Overall Summary:** The individual file summaries, along with the PR title and body, are fed to the Gemini API to generate a comprehensive, high-level overview of the entire Pull Request.
9.  **Post PR Comment:** The final structured summary, including change statistics, `sorry` tracking, style adherence report (if applicable), and per-file summaries, is posted as a comment on the Pull Request. If a previous summary comment exists, it will be updated.
10. **Clean up:** The temporary `pr.diff` file is removed.

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
          # Optional: Comma-separated list of keywords to track for sorrys (default: 'def,abbrev,example,theorem,opaque,lemma,instance')
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
| `lean_keywords`| A comma-separated list of keywords to track for `sorry`s in `.lean` files. | `false` | `def,abbrev,example,theorem,opaque,lemma,instance` |
| `style_guide_path`| Optional: Path to a style guide file within the repository for adherence checking. | `false` | `CONTRIBUTING.md` |

## Customizing AI Prompts

The intelligence and behavior of the AI summarizer are primarily governed by Markdown prompt templates stored in the `prompts/` directory within this action.

*   `summarize_file.md`: Contains the instructions for the AI when generating a concise summary for individual files.
*   `synthesize_summary.md`: Guides the AI in generating the overall high-level summary from the per-file summaries.
*   `check_style.md`: Provides the rules and context for the AI to check code changes against the specified style guide.

You can modify these `.md` files directly within your forked repository to fine-tune the AI's persona, summarization criteria, style checking rules, or desired output format. Placeholders like `` `{{FILE_PATH}}` ``, `` `{{FILE_DIFF}}` ``, `` `{{PR_TITLE}}` ``, `` `{{PR_BODY}}` ``, `` `{{PER_FILE_SUMMARIES}}` ``, `` `{{STYLE_GUIDE_CONTENT}}` ``, and `` `{{DIFF_CONTENT}}` `` are used to inject dynamic information into the prompts during runtime. Ensure these placeholders are kept intact if you wish the AI to receive the corresponding context.

## License

This project is licensed under the [Apache License 2.0](LICENSE).
