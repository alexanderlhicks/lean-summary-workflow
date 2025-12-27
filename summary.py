import os
import re
import sys
from datetime import datetime
import google.generativeai as genai
from github import Github, Auth

# --- Constants ---
MAX_DIFF_TOKENS = 1_500_000
COMMENT_IDENTIFIER = "<!-- gemini-pr-summary-{{timestamp}} -->"

# --- AI Generation ---

def _call_gemini(prompt, model_name):
    """A helper function to call the Gemini API and handle errors."""
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt, request_options={'timeout': 180})
        return response.text
    except Exception as e:
        print(f"Warning: Gemini API call failed. {e}")
        return f"Error: {e}"

def _read_prompt_template(template_name: str) -> str:
    action_path = os.path.dirname(os.path.realpath(__file__))
    prompt_template_path = os.path.join(action_path, "prompts", template_name)
    try:
        with open(prompt_template_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        sys.exit(f"Error: Prompt template not found at {prompt_template_path}")

def split_diff_into_files(diff_content):
    """Splits a full git diff into a dictionary of per-file diffs."""
    files = {}
    file_diffs = re.split(r'(?=diff --git a/.+ b/.+)', diff_content)
    for file_diff in file_diffs:
        if not file_diff.strip(): continue
        match = re.search(r'diff --git a/(.+) b/(.+)', file_diff)
        if match:
            files[match.group(2)] = file_diff
    return files

def summarize_file_diff(file_path, file_diff, model_name):
    """Generates a summary for a single file's diff (Map step)."""
    prompt_template = _read_prompt_template("summarize_file.txt")
    prompt = prompt_template.replace("{{FILE_PATH}}", file_path).replace("{{FILE_DIFF}}", file_diff)
    return _call_gemini(prompt, model_name)

def synthesize_summary(per_file_summaries, model_name, pr_title, pr_body):
    """Synthesizes a final summary from per-file summaries (Reduce step)."""
    summaries_text = "\n".join(f"- {s}" for s in per_file_summaries)
    prompt_template = _read_prompt_template("synthesize_summary.md")
    prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                            .replace("{{PR_BODY}}", pr_body) \
                            .replace("{{PER_FILE_SUMMARIES}}", summaries_text)
    return _call_gemini(prompt, model_name)

def check_style_adherence(diff_content, style_guide_content, model_name):
    """Checks the diff against a style guide."""
    if not style_guide_content:
        return None
    prompt_template = _read_prompt_template("check_style.md")
    prompt = prompt_template.replace("{{STYLE_GUIDE_CONTENT}}", style_guide_content) \
                            .replace("{{DIFF_CONTENT}}", diff_content)
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
        if line.startswith('+'): self._new_line_num += 1
        elif line.startswith('-'): self._old_line_num += 1
        else:
            self._old_line_num += 1
            self._new_line_num += 1

    def _track_sorries(self, line):
        stripped_line = line.lstrip('+- ')
        if any(stripped_line.startswith(keyword + ' ') for keyword in self._sorry_keywords):
            self._current_decl_header = re.sub(r"^[+-]\s*", "", line)
            name_match = self._name_extract_regex.match(self._current_decl_header)
            if name_match: self._current_decl_name = name_match.group(1)
        if 'sorry' in line and self._current_decl_name:
            comment_pos = line.find("--")
            sorry_pos = line.find("sorry")
            if comment_pos != -1 and sorry_pos > comment_pos: return
            stable_id = f"{self._current_decl_name}@{self._current_file}"
            sorry_info = {'id': stable_id, 'file': self._current_file, 'name': self._current_decl_name, 'header': self._current_decl_header.split(":=")[0].strip()}
            if line.startswith('+'):
                sorry_info['line'] = self._new_line_num
                self._raw_added[stable_id] = sorry_info
            elif line.startswith('-'):
                sorry_info['line'] = self._old_line_num
                self._raw_removed[stable_id] = sorry_info
    
    def _categorize_sorries(self):
        added_ids, removed_ids = set(self._raw_added.keys()), set(self._raw_removed.keys())
        affected_ids = added_ids.intersection(removed_ids)
        for sid in affected_ids: self.affected_sorries.append({'id': sid, 'file': self._raw_added[sid]['file'], 'context': self._raw_added[sid]['header'], 'old_line': self._raw_removed[sid]['line'], 'new_line': self._raw_added[sid]['line']})
        for sid in added_ids - affected_ids: self.added_sorries.append(f"`{self._raw_added[sid]['header']}` in `{self._raw_added[sid]['file']}`")
        for sid in removed_ids - affected_ids: self.removed_sorries.append(f"`{self._raw_removed[sid]['header']}` in `{self._raw_removed[sid]['file']}`")

# --- Comment Formatting ---
def format_summary(ai_summary, stats, added, removed, affected, truncated, issues, per_file_summaries, style_report):
    """Formats the final summary comment in Markdown."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S")
    comment_id = COMMENT_IDENTIFIER.replace("{{timestamp}}", timestamp)
    summary = f"### 🤖 Gemini PR Summary\n\n{comment_id}\n\n{ai_summary}\n"
    if truncated: summary += "> *Note: The diff was too large and was truncated.*\n"
    summary += f"\n---\n\n**Analysis of Changes**\n\n| Metric | Count |\n| --- | --- |\n| 📝 **Files Changed** | {stats['files_changed']} |\n| ✅ **Lines Added** | {stats['lines_added']} |\n| ❌ **Lines Removed** | {stats['lines_removed']} |\n"
    summary += "\n---\n\n**`sorry` Tracking**\n\n"
    if removed: summary += f"<details><summary>✅ **Removed:** {len(removed)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in removed) + "</details>\n"
    if added: summary += f"<details><summary>❌ **Added:** {len(added)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in added) + "</details>\n"
    if affected:
        summary += f"<details><summary>✏️ **Affected:** {len(affected)} `sorry`(s) (line number changed)</summary>\n\n"
        for s in affected:
            issue_link = next((f" (Issue #{issue.number})" for issue in issues if issue.body and f"<!-- sorry-tracker-id: {s['id']} -->" in issue.body), "")
            summary += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}{issue_link}\n"
        summary += "</details>\n"
    if not any([added, removed, affected]): summary += "*   No `sorry`s were added, removed, or affected.\n"
    if style_report: summary += f"\n---\n\n<details><summary>🎨 **Style Guide Adherence**</summary>\n\n{style_report}\n</details>\n"
    if per_file_summaries: summary += f"\n---\n\n<details><summary>📄 **Per-File Summaries**</summary>\n\n" + "".join(f"*   {s}\n" for s in per_file_summaries) + "</details>\n"
    summary += f"\n---\n\n*Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.*"
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
    comment_regex = re.compile(COMMENT_IDENTIFIER.replace("{{timestamp}}", ".*?"))
    existing_comment = next((c for c in pr.get_issue_comments() if comment_regex.search(c.body)), None)
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
        sys.exit("Error: GEMINI_API_KEY environment variable not set.")
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    model_name = os.environ.get("INPUT_GEMINI_MODEL", 'gemini-3-pro-preview')
    keywords = [k.strip() for k in os.environ.get("INPUT_LEAN_KEYWORDS", 'def,abbrev,example,theorem,opaque,lemma,instance').split(',')]
    style_guide_path = os.environ.get("INPUT_STYLE_GUIDE_PATH")

    try:
        with open("pr.diff", "r") as f:
            diff = f.read()
    except FileNotFoundError:
        sys.exit("Error: pr.diff not found.")

    analyzer = DiffAnalyzer(keywords)
    stats, added, removed, affected = analyzer.analyze(diff)

    repo, pr, issues, pr_title, pr_body = None, None, [], "", ""
    if "GITHUB_TOKEN" in os.environ:
        repo, pr = get_github_objects(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], int(os.environ["PR_NUMBER"]))
        issues, pr_title, pr_body = find_sorry_issues(repo), pr.title, pr.body or ""

    truncated = len(diff) > MAX_DIFF_TOKENS
    if truncated: diff = diff[:MAX_DIFF_TOKENS]

    style_guide_content = ""
    if style_guide_path:
        try:
            with open(style_guide_path, "r") as f:
                style_guide_content = f.read()
        except FileNotFoundError:
            print(f"Warning: Style guide file not found at {style_guide_path}")

    style_report = check_style_adherence(diff, style_guide_content, model_name) if style_guide_content else None

    diff_by_file = split_diff_into_files(diff)
    per_file_summaries = [f"**{fp}**: {summarize_file_diff(fp, fd, model_name).strip()}" for fp, fd in diff_by_file.items()]

    try:
        ai_summary = synthesize_summary(per_file_summaries, model_name, pr_title, pr_body)
    except Exception as e:
        raise RuntimeError(f"Error synthesizing final summary: {e}")

    final_summary = format_summary(ai_summary, stats, added, removed, affected, truncated, issues, per_file_summaries, style_report)
    
    if pr:
        post_github_comment(pr, final_summary)
    else:
        print("Not in a GitHub Actions context. Printing summary instead:\n", final_summary)

if __name__ == "__main__":
    main()
