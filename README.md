# PR Summary Action

This GitHub Action generates a concise, high-level summary for a pull request using the Google Gemini API. It analyzes the PR's title, body, and git diff to produce a structured summary, and includes special features for analyzing [Lean](https://lean-lang.org/) projects, such as tracking `sorry`s.

For large pull requests, the action uses a hierarchical "MapReduce" approach: it first summarizes each file's changes individually and then synthesizes those into a final, high-level overview. This ensures that even large, complex changes can be summarized accurately.



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
```

## Inputs

| Input | Description | Required | Default |
|---|---|---|---|
| `github_token` | The GitHub token for API calls. Should be set to `${{ secrets.GITHUB_TOKEN }}`. | `true` | |
| `gemini_api_key` | The API key for the Gemini API, used for summary generation. Store this as a secret in your repository. | `true` | |
| `github_repository` | The GitHub repository in the format `owner/repo`. Should be set to `${{ github.repository }}`. | `true` | |
| `pr_number` | The pull request number. Should be set to `${{ github.event.pull_request.number }}`. | `true` | |
| `gemini_model` | The Gemini model to use for the summary. | `false` | `gemini-3-pro-preview` |
| `lean_keywords`| A comma-separated list of keywords to track for `sorry`s in `.lean` files. | `false` | `def,abbrev,example,theorem,opaque,lemma,instance` |

## License

This project is licensed under the [Apache License 2.0](LICENSE).
