import os
import re
import sys
from datetime import datetime
import google.generativeai as genai
from github import Github, Auth

# --- Constants ---
MAX_DIFF_TOKENS = 1_500_000
COMMENT_IDENTIFIER = "<!-- gemini-pr-summary-{{timestamp}} -->"

# --- AI Summary Generation ---

def _call_gemini(prompt, model_name):
    """A helper function to call the Gemini API and handle errors."""
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt, request_options={'timeout': 180})
        return response.text
    except Exception as e:
        # For individual file summaries, we don't want to fail the whole run.
        # For the final synthesis, the error will be caught by the main block's caller.
        print(f"Warning: Gemini API call failed. {e}")
        return f"Error summarizing: {e}"

def split_diff_into_files(diff_content):
    """Splits a full git diff into a dictionary of per-file diffs."""
    files = {}
    # Use a regex to split the diff by the file header, keeping the header
    file_diffs = re.split(r'(?=diff --git a/.+ b/.+)', diff_content)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        match = re.search(r'diff --git a/(.+) b/(.+)', file_diff)
        if match:
            # The 'b' path is the new or current path of the file
            file_path = match.group(2)
            files[file_path] = file_diff
    return files

def summarize_file_diff(file_path, file_diff, model_name):
    """Generates a summary for a single file's diff (Map step)."""
    prompt = f"""
    Please provide a concise, one-sentence summary for the changes in the file `{file_path}`.
    Focus on the primary purpose of the changes.

    Diff:
    {file_diff}
    """
    return _call_gemini(prompt, model_name)

def synthesize_summary(per_file_summaries, model_name, pr_title, pr_body):
    """Synthesizes a final summary from a list of per-file summaries (Reduce step)."""
    summaries_text = "\n".join(f"- {s}" for s in per_file_summaries)

    prompt = f"""
You are a senior software engineer summarizing a pull request for a code review.
Based on the PR title, body, and the following per-file summaries, please provide a structured, high-level summary of the entire pull request.
    Categorize the changes into the following sections:
    - **Features**: New functionality added.
    - **Fixes**: Bug fixes.
    - **Refactoring**: Code improvements without changing behavior.
    - **Documentation**: Changes to comments or documentation files.

    PR Title: {pr_title}
    PR Body:
    {pr_body}

    Per-File Summaries:
    {summaries_text}
    """
    return _call_gemini(prompt, model_name)

# --- Diff Analysis ---
class DiffAnalyzer:
    """Parses a git diff to extract statistics and track 'sorry's."""

    def __init__(self, sorry_keywords):
        self.files_changed = set()
        self.lines_added = 0
        self.lines_removed = 0
        self.added_sorries = []
        self.removed_sorries = []
        self.affected_sorries = []
        self._sorry_keywords = sorry_keywords

        self._current_file = ""
        self._old_line_num = 0
        self._new_line_num = 0
        self._current_decl_header = ""
        self._current_decl_name = ""
        self._raw_added = {}
        self._raw_removed = {}

        self._file_path_regex = re.compile(r'diff --git a/(.+) b/(.+)')
        self._hunk_header_regex = re.compile(r'@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')
        keywords_regex_part = "|".join(re.escape(k) for k in self._sorry_keywords)
        self._name_extract_regex = re.compile(
            r".*??(?:{})\s+([^\s\(\{:]+)".format(keywords_regex_part)
        )

    def analyze(self, diff):
        """Analyzes the diff and returns the results."""
        for line in diff.splitlines():
            if self._parse_file_header(line) or self._parse_hunk_header(line):
                continue

            if line.startswith("---") or line.startswith("+++"):
                continue

            if not self._current_file.endswith(".lean"):
                continue

            self._process_line(line)

        self._categorize_sorries()

        stats = {
            "files_changed": len(self.files_changed),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }
        return stats, self.added_sorries, self.removed_sorries, self.affected_sorries

    def _parse_file_header(self, line):
        match = self._file_path_regex.match(line)
        if match:
            self._current_file = match.group(2)
            self.files_changed.add(self._current_file)
            self._current_decl_header = ""
            self._current_decl_name = ""
            return True
        return False

    def _parse_hunk_header(self, line):
        match = self._hunk_header_regex.match(line)
        if match:
            self._old_line_num = int(match.group(1))
            self._new_line_num = int(match.group(3))
            self._current_decl_header = ""
            self._current_decl_name = ""
            return True
        return False

    def _process_line(self, line):
        if line.startswith('+'):
            self.lines_added += 1
        elif line.startswith('-'):
            self.lines_removed += 1

        self._track_sorries(line)

        if line.startswith('+'):
            self._new_line_num += 1
        elif line.startswith('-'):
            self._old_line_num += 1
        else:
            self._old_line_num += 1
            self._new_line_num += 1

    def _track_sorries(self, line):
        stripped_line = line.lstrip('+- ')
        if any(stripped_line.startswith(keyword + ' ') for keyword in self._sorry_keywords):
            self._current_decl_header = re.sub(r"^[+-]\s*", "", line)
            name_match = self._name_extract_regex.match(self._current_decl_header)
            if name_match:
                self._current_decl_name = name_match.group(1)

        if 'sorry' in line and self._current_decl_name:
            comment_pos = line.find("--")
            sorry_pos = line.find("sorry")
            if comment_pos != -1 and sorry_pos > comment_pos:
                return

            stable_id = f"{self._current_decl_name}@{self._current_file}"
            sorry_info = {
                'id': stable_id,
                'file': self._current_file,
                'name': self._current_decl_name,
                'header': self._current_decl_header.split(":=")[0].strip()
            }

            if line.startswith('+'):
                sorry_info['line'] = self._new_line_num
                self._raw_added[stable_id] = sorry_info
            elif line.startswith('-'):
                sorry_info['line'] = self._old_line_num
                self._raw_removed[stable_id] = sorry_info
    
    def _categorize_sorries(self):
        added_ids = set(self._raw_added.keys())
        removed_ids = set(self._raw_removed.keys())
        affected_ids = added_ids.intersection(removed_ids)

        for sid in affected_ids:
            self.affected_sorries.append({
                'id': sid,
                'file': self._raw_added[sid]['file'],
                'context': self._raw_added[sid]['header'],
                'old_line': self._raw_removed[sid]['line'],
                'new_line': self._raw_added[sid]['line']
            })

        for sid in added_ids - affected_ids:
            self.added_sorries.append(f"`{self._raw_added[sid]['header']}` in `{self._raw_added[sid]['file']}`")

        for sid in removed_ids - affected_ids:
            self.removed_sorries.append(f"`{self._raw_removed[sid]['header']}` in `{self._raw_removed[sid]['file']}`")

# --- Comment Formatting ---
def format_summary(ai_summary, stats, added_sorries, removed_sorries, affected_sorries, truncated, issues, per_file_summaries):
    """Formats the final summary comment in Markdown."""
    
    timestamp_str = datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S")
    unique_comment_identifier = COMMENT_IDENTIFIER.replace("{{timestamp}}", timestamp_str)
    summary = f"### 🤖 Gemini PR Summary\n\n{unique_comment_identifier}\n\n"
    summary += f"{ai_summary}\n"
    if truncated:
        summary += "> *Note: The diff was too large to be fully analyzed and was truncated.*\\n"
    
    summary += "\n---\n\n"
    summary += "**Analysis of Changes**\n\n"
    summary += "| Metric | Count |\n| --- | --- |\n"
    summary += f"| 📝 **Files Changed** | {stats['files_changed']} |\n"
    summary += f"| ✅ **Lines Added** | {stats['lines_added']} |\n"
    summary += f"| ❌ **Lines Removed** | {stats['lines_removed']} |\n"

    summary += "\n---\n\n"
    summary += "**`sorry` Tracking**\n\n"
    
    if removed_sorries:
        summary += f"<details><summary>✅ **Removed:** {len(removed_sorries)} `sorry`(s)</summary>\n\n"
        for sorry in removed_sorries:
            summary += f"*   {sorry}\n"
        summary += "</details>\n"
    
    if added_sorries:
        summary += f"<details><summary>❌ **Added:** {len(added_sorries)} `sorry`(s)</summary>\n\n"
        for sorry in added_sorries:
            summary += f"*   {sorry}\n"
        summary += "</details>\n"

    if affected_sorries:
        summary += f"<details><summary>✏️ **Affected:** {len(affected_sorries)} `sorry`(s) (line number changed)</summary>\n\n"
        for sorry in affected_sorries:
            # Find the corresponding issue by searching for the stable ID in the issue body
            issue_link = ""
            stable_id_comment = f"<!-- sorry-tracker-id: {sorry['id']} -->"
            for issue in issues:
                if issue.body and stable_id_comment in issue.body:
                    issue_link = f" (Issue #{issue.number})"
                    break
            summary += f"*   `{sorry['context']}` in `{sorry['file']}` moved from L{sorry['old_line']} to L{sorry['new_line']}{issue_link}\n"
        summary += "</details>\n"

    if not added_sorries and not removed_sorries and not affected_sorries:
        summary += "*   No `sorry`s were added, removed, or affected.\n"

    # --- Append Per-File Summaries ---
    if per_file_summaries:
        summary += "\n---\n\n"
        summary += "<details><summary>📄 **Per-File Summaries**</summary>\n\n"
        for file_summary in per_file_summaries:
            summary += f"*   {file_summary}\n"
        summary += "</details>\n"

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    summary += f"\n---\n\n*Last updated: {timestamp}. See the [main CI run](https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions) for build status.*"
    
    return summary


def find_sorry_issues(repo):
    """Finds all open issues with the 'proof wanted' label."""
    try:
        return repo.get_issues(state="open", labels=["proof wanted"])
    except Exception as e:
        print(f"Warning: Could not fetch issues. {e}")
        return []

# --- GitHub Interaction ---
def get_github_objects(token, repo_name, pr_number):
    """Initializes and returns the GitHub repo and PR objects."""
    auth = Auth.Token(token)
    g = Github(auth=auth)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    return repo, pr

def post_github_comment(pr, summary):
    """Finds and updates an existing comment or creates a new one."""
    existing_comment = None
    comment_regex = re.compile(COMMENT_IDENTIFIER.replace("{{timestamp}}", ".*?"))
    for comment in pr.get_issue_comments():
        if comment_regex.search(comment.body):
            existing_comment = comment
            break
    
    if existing_comment:
        existing_comment.edit(summary)
        print("Updated existing comment.")
    else:
        pr.create_issue_comment(summary)
        print("Created a new comment.")

# --- Main Execution ---
def main():
    """Main execution block."""
    if "GEMINI_API_KEY" not in os.environ:
        print("Error: GEMINI_API_KEY environment variable not set.")
        sys.exit(1)
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    gemini_model_name = os.environ.get("INPUT_GEMINI_MODEL", 'gemini-3-pro-preview')
    lean_keywords_str = os.environ.get("INPUT_LEAN_KEYWORDS", 'def,abbrev,example,theorem,opaque,lemma,instance')
    lean_keywords = [k.strip() for k in lean_keywords_str.split(',')]

    try:
        with open("pr.diff", "r") as f:
            diff = f.read()
    except FileNotFoundError:
        print("Error: pr.diff not found.")
        sys.exit(1)

    analyzer = DiffAnalyzer(lean_keywords)
    stats, added_sorries, removed_sorries, affected_sorries = analyzer.analyze(diff)

    pr_title = ""
    pr_body = ""
    issues = []
    repo = None
    pr = None

    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        repo_name = os.environ["GITHUB_REPOSITORY"]
        pr_number = int(os.environ["PR_NUMBER"])
        repo, pr = get_github_objects(github_token, repo_name, pr_number)
        issues = find_sorry_issues(repo)
        pr_title = pr.title
        pr_body = pr.body or ""

    # --- Hierarchical Summary Generation ---
    truncated = len(diff) > MAX_DIFF_TOKENS
    if truncated:
        diff = diff[:MAX_DIFF_TOKENS]

    # "Map" step: Summarize each file's diff
    diff_by_file = split_diff_into_files(diff)
    per_file_summaries = []
    for file_path, file_diff in diff_by_file.items():
        summary = summarize_file_diff(file_path, file_diff, gemini_model_name)
        per_file_summaries.append(f"**{file_path}**: {summary.strip()}")

    # "Reduce" step: Synthesize a final summary
    try:
        ai_summary = synthesize_summary(per_file_summaries, gemini_model_name, pr_title, pr_body)
    except Exception as e:
        raise RuntimeError(f"Error synthesizing final summary: {e}")

    final_summary = format_summary(ai_summary, stats, added_sorries, removed_sorries, affected_sorries, truncated, issues, per_file_summaries)
    
    if pr:
        post_github_comment(pr, final_summary)
    else:

        print("Not in GitHub Actions context. Printing summary instead of posting:")
        print(final_summary)

if __name__ == "__main__":
    main()
