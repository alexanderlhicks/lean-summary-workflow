import os
import re
import sys
import json
import hashlib
import concurrent.futures
import time
from datetime import datetime
from collections import defaultdict
from google import genai
from github import Github, Auth
from github.PullRequest import PullRequest
from github.Repository import Repository

# --- Constants ---
MAX_DIFF_CHARS = 1_500_000
COMMENT_IDENTIFIER = "<!-- gemini-pr-summary-{{timestamp}} -->"
CACHE_IDENTIFIER = "<!-- gemini-cache: "

# --- Global Client ---
_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            sys.exit("Error: GEMINI_API_KEY environment variable not set.")
        _client = genai.Client(api_key=api_key)
    return _client

# --- AI Generation ---

def _call_gemini(prompt, model_name, response_mime_type=None, retries=3, backoff_factor=2):
    """A helper function to call the Gemini API with retry logic."""
    client = get_client()
    kwargs = {}
    if response_mime_type:
        kwargs["config"] = {"response_mime_type": response_mime_type}
    
    for i in range(retries):
        try:
            response = client.models.generate_content(model=model_name, contents=prompt, **kwargs)
            return response.text
        except Exception as e:
            if i == retries - 1:
                print(f"Error: Gemini API call failed after {retries} attempts: {e}")
                return None
            wait_time = backoff_factor ** (i + 1)
            print(f"Warning: Gemini API call failed ({e}). Retrying in {wait_time}s...")
            time.sleep(wait_time)
    return None

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
    file_diffs = re.split(r'^diff --git ', diff_content, flags=re.MULTILINE)
    for file_diff in file_diffs:
        if not file_diff.strip(): continue
        # Re-add the split marker
        full_file_diff = "diff --git " + file_diff
        match = re.search(r'^diff --git a/(.+) b/(.+)', full_file_diff, flags=re.MULTILINE)
        if match:
            files[match.group(2)] = full_file_diff
    return files

def summarize_file_diff(file_path, file_diff, model_name, prompt_template):
    """Generates a summary for a single file's diff (Map step)."""
    prompt = prompt_template.replace("{{FILE_PATH}}", file_path).replace("{{FILE_DIFF}}", file_diff)
    return _call_gemini(prompt, model_name)

def synthesize_summary(per_file_summaries, model_name, pr_title, pr_body):
    """Synthesizes a final summary from per-file summaries (Reduce step)."""
    summaries_text = "\n".join(f"- {s}" for s in per_file_summaries)
    prompt_template = _read_prompt_template("synthesize_summary.md")
    prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                            .replace("{{PR_BODY}}", pr_body) \
                            .replace("{{PER_FILE_SUMMARIES}}", summaries_text)
    result = _call_gemini(prompt, model_name)
    if not result:
        raise RuntimeError("Failed to synthesize PR summary from per-file summaries.")
    return result

def check_style_adherence(diff_content, style_guide_content, model_name, prompt_template):
    """Checks the diff against a style guide."""
    if not style_guide_content:
        return None
    prompt = prompt_template.replace("{{STYLE_GUIDE_CONTENT}}", style_guide_content) \
                            .replace("{{DIFF_CONTENT}}", diff_content)
    return _call_gemini(prompt, model_name)

def triage_files(file_paths, diff_by_file, model_name):
    """Uses the AI to filter out noise files before summarization."""
    if not file_paths:
        return []
    
    file_list_with_counts = []
    for fp in file_paths:
        diff = diff_by_file[fp]
        # Count lines starting with + or - (excluding the diff headers which start with +++ or ---)
        added = sum(1 for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
        removed = sum(1 for line in diff.splitlines() if line.startswith('-') and not line.startswith('---'))
        file_list_with_counts.append(f"{fp} (+{added}/-{removed})")
        
    file_list_str = "\n".join(file_list_with_counts)
    prompt_template = _read_prompt_template("triage.md")
    prompt = prompt_template.replace("{{FILE_LIST}}", file_list_str)
    
    response = _call_gemini(prompt, model_name, response_mime_type="application/json")
    if not response:
        print("Warning: Triage agent failed. Proceeding with all files.")
        return file_paths
    try:
        # Strip markdown code block formatting if present
        clean_response = response.strip()
        if clean_response.startswith("```"):
            lines = clean_response.splitlines()
            if len(lines) > 2:
                clean_response = "\n".join(lines[1:-1])
            else:
                clean_response = clean_response.strip("`").removeprefix("json").strip()

        filtered_files = json.loads(clean_response)
        if isinstance(filtered_files, list):
            return [f for f in filtered_files if f in file_paths]
        return file_paths
    except json.JSONDecodeError:
        print(f"Warning: Triage agent returned invalid JSON: {response}. Proceeding with all files.")
        return file_paths

def refine_summary(draft_summary, pr_title, pr_body, model_name):
    """Refines the final summary using the AI."""
    prompt_template = _read_prompt_template("refine_summary.md")
    prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                            .replace("{{PR_BODY}}", pr_body) \
                            .replace("{{DRAFT_SUMMARY}}", draft_summary)
    result = _call_gemini(prompt, model_name)
    if not result:
        print("Warning: Refiner agent failed. Falling back to draft summary.")
        return draft_summary
    return result

# --- Diff Analysis ---
class DiffAnalyzer:
    """Parses a git diff to extract statistics and track 'sorry's."""

    def __init__(self, decl_keywords):
        self.files_changed = set()
        self.lines_added = 0
        self.lines_removed = 0
        self.added_sorries = []
        self.removed_sorries = []
        self.affected_sorries = []
        self.added_decls = []
        self.removed_decls = []
        self.affected_decls = []
        self._decl_keywords = decl_keywords

        self._current_file = ""
        self._old_line_num = 0
        self._new_line_num = 0
        self._current_decl_header = ""
        self._current_decl_name = ""
        self._raw_added = {}
        self._raw_removed = {}
        self._raw_added_decls = {}
        self._raw_removed_decls = {}

        self._file_path_regex = re.compile(r'diff --git a/(.+) b/(.+)')
        self._hunk_header_regex = re.compile(r'@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')
        keywords_regex_part = "|".join(re.escape(k) for k in self._decl_keywords)
        self._name_extract_regex = re.compile(
            r".*?(?:{})\s+([^\s\(\{{:]+)".format(keywords_regex_part)
        )

    def analyze(self, diff):
        """Analyzes the diff and returns the results."""
        for line in diff.splitlines():
            if self._parse_file_header(line) or self._parse_hunk_header(line):
                continue

            if line.startswith("---") or line.startswith("+++"):
                continue

            # Stats are now collected for ALL files
            if line.startswith('+'):
                self.lines_added += 1
            elif line.startswith('-'):
                self.lines_removed += 1

            # Lean-specific analysis
            if self._current_file.endswith(".lean"):
                self._process_line(line)
            else:
                # Still need to increment line numbers for non-Lean files 
                # if we were tracking something in them, but we aren't.
                pass

        self._categorize_sorries()
        self._categorize_decls()

        stats = {
            "files_changed": len(self.files_changed),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }
        return stats, self.added_sorries, self.removed_sorries, self.affected_sorries, self.added_decls, self.removed_decls, self.affected_decls

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
        self._track_sorries_and_decls(line)
        if line.startswith('+'): self._new_line_num += 1
        elif line.startswith('-'): self._old_line_num += 1
        else:
            self._old_line_num += 1
            self._new_line_num += 1

    def _track_sorries_and_decls(self, line):
        # Strip diff markers and leading whitespace
        content = line[1:] if line.startswith(('+', '-', ' ')) else line
        stripped_content = content.lstrip()
        
        # Track declarations
        if any(stripped_content.startswith(keyword + ' ') for keyword in self._decl_keywords):
            self._current_decl_header = stripped_content
            name_match = self._name_extract_regex.match(self._current_decl_header)
            if name_match:
                self._current_decl_name = name_match.group(1)
                # Use name + file as ID for categorization, but we might have multiple sorries
                stable_id = f"{self._current_decl_name}@{self._current_file}"
                decl_info = {
                    'id': stable_id, 
                    'file': self._current_file, 
                    'name': self._current_decl_name, 
                    'header': self._current_decl_header.split(":=")[0].strip()
                }
                if line.startswith('+'):
                    decl_info['line'] = self._new_line_num
                    self._raw_added_decls[stable_id] = decl_info
                elif line.startswith('-'):
                    decl_info['line'] = self._old_line_num
                    self._raw_removed_decls[stable_id] = decl_info

        # Track sorries
        if 'sorry' in content and self._current_decl_name:
            # Improved comment detection: -- must be at start of line or preceded by whitespace
            comment_match = re.search(r'(?:^|\s)--', content)
            sorry_pos = content.find("sorry")
            if comment_match and sorry_pos > comment_match.start():
                return
            
            stable_id = f"{self._current_decl_name}@{self._current_file}"
            # Unique key for each sorry instance to avoid overwriting
            line_num = self._new_line_num if line.startswith('+') else self._old_line_num
            instance_key = f"{stable_id}#L{line_num}"
            
            sorry_info = {
                'id': stable_id, 
                'file': self._current_file, 
                'name': self._current_decl_name, 
                'header': self._current_decl_header.split(":=")[0].strip() if self._current_decl_header else "unknown"
            }
            if line.startswith('+'):
                sorry_info['line'] = self._new_line_num
                self._raw_added[instance_key] = sorry_info
            elif line.startswith('-'):
                sorry_info['line'] = self._old_line_num
                self._raw_removed[instance_key] = sorry_info
    
    def _categorize_decls(self):
        added_ids, removed_ids = set(self._raw_added_decls.keys()), set(self._raw_removed_decls.keys())
        affected_ids = added_ids.intersection(removed_ids)
        for sid in affected_ids:
            self.affected_decls.append({'id': sid, 'file': self._raw_added_decls[sid]['file'], 'context': self._raw_added_decls[sid]['header'], 'old_line': self._raw_removed_decls[sid]['line'], 'new_line': self._raw_added_decls[sid]['line']})
        for sid in added_ids - affected_ids:
            self.added_decls.append(f"`{self._raw_added_decls[sid]['header']}` in `{self._raw_added_decls[sid]['file']}`")
        for sid in removed_ids - affected_ids:
            self.removed_decls.append(f"`{self._raw_removed_decls[sid]['header']}` in `{self._raw_removed_decls[sid]['file']}`")

    def _categorize_sorries(self):
        added_by_id = defaultdict(list)
        removed_by_id = defaultdict(list)
        
        for info in self._raw_added.values():
            added_by_id[info['id']].append(info)
            
        for info in self._raw_removed.values():
            removed_by_id[info['id']].append(info)
            
        all_ids = set(added_by_id.keys()).union(removed_by_id.keys())
        
        for sid in all_ids:
            adds = added_by_id[sid]
            rems = removed_by_id[sid]
            
            # Match up to min(len(adds), len(rems)) as "affected"
            match_count = min(len(adds), len(rems))
            
            for i in range(match_count):
                added_info = adds[i]
                removed_info = rems[i]
                self.affected_sorries.append({
                    'id': added_info['id'], 
                    'file': added_info['file'], 
                    'context': added_info['header'], 
                    'old_line': removed_info['line'], 
                    'new_line': added_info['line']
                })
                
            # Remaining are purely added or removed
            for i in range(match_count, len(adds)):
                info = adds[i]
                self.added_sorries.append(f"`{info['header']}` in `{info['file']}` (L{info['line']})")
                
            for i in range(match_count, len(rems)):
                info = rems[i]
                self.removed_sorries.append(f"`{info['header']}` in `{info['file']}` (L{info['line']})")

# --- Caching ---
class SummaryCache:
    """Handles caching of file diff summaries."""
    def __init__(self, pr: PullRequest):
        self._cache = self._load_from_comment(pr)

    def _load_from_comment(self, pr: PullRequest):
        comment = find_existing_comment(pr)
        if comment and CACHE_IDENTIFIER in comment.body:
            try:
                cache_str = comment.body.split(CACHE_IDENTIFIER, 1)[1].split("-->", 1)[0]
                return json.loads(cache_str)
            except (IndexError, json.JSONDecodeError):
                return {}
        return {}

    def get(self, file_path, file_diff_hash):
        if file_path in self._cache and self._cache[file_path]['hash'] == file_diff_hash:
            return self._cache[file_path]['summary']
        return None

    def update(self, file_path, file_diff_hash, summary):
        self._cache[file_path] = {'hash': file_diff_hash, 'summary': summary}

    def to_json(self):
        return json.dumps(self._cache)

# --- Comment Formatting ---

def _format_stats_section(stats):
    return (
        "\n---\n\n**Statistics**\n\n"
        "| Metric | Count |\n"
        "| --- | --- |\n"
        f"| 📝 **Files Changed** | {stats['files_changed']} |\n"
        f"| ✅ **Lines Added** | {stats['lines_added']} |\n"
        f"| ❌ **Lines Removed** | {stats['lines_removed']} |\n"
    )

def _format_decls_section(added, removed, affected):
    res = "\n---\n\n**Lean Declarations**\n\n"
    if removed:
        res += f"<details><summary>✏️ **Removed:** {len(removed)} declaration(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in removed) + "</details>\n"
    if added:
        res += f"<details><summary>✏️ **Added:** {len(added)} declaration(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in added) + "</details>\n"
    if affected:
        res += f"<details><summary>✏️ **Affected:** {len(affected)} declaration(s) (line number changed)</summary>\n\n"
        for s in affected:
            res += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}\n"
        res += "</details>\n"
    if not any([added, removed, affected]):
        res += "*   No declarations were added, removed, or affected.\n"
    return res

def _format_sorry_section(added, removed, affected, issues):
    res = "\n---\n\n**`sorry` Tracking**\n\n"
    if removed:
        res += f"<details><summary>✅ **Removed:** {len(removed)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in removed) + "</details>\n"
    if added:
        res += f"<details><summary>❌ **Added:** {len(added)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in added) + "</details>\n"
    if affected:
        res += f"<details><summary>✏️ **Affected:** {len(affected)} `sorry`(s) (line number changed)</summary>\n\n"
        for s in affected:
            issue_link = next((f" (Issue #{issue.number})" for issue in issues if issue.body and f"<!-- sorry-tracker-id: {s['id']} -->" in issue.body), "")
            res += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}{issue_link}\n"
        res += "</details>\n"
    if not any([added, removed, affected]):
        res += "*   No `sorry`s were added, removed, or affected.\n"
    return res

def format_summary(ai_summary, stats, added, removed, affected, added_decls, removed_decls, affected_decls, truncated, issues, per_file_summaries, style_report, cache):
    """Formats the final summary comment in Markdown."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S")
    comment_id = COMMENT_IDENTIFIER.replace("{{timestamp}}", timestamp)
    cache_html = f"{CACHE_IDENTIFIER}{cache.to_json()}-->\n\n" if cache else ""
    
    summary = f"### 🤖 Gemini PR Summary\n\n{comment_id}\n\n{cache_html}{ai_summary}\n"
    
    if truncated:
        summary += "> *Note: The diff was too large and was truncated.*\n"
    
    summary += _format_stats_section(stats)
    summary += _format_decls_section(added_decls, removed_decls, affected_decls)
    summary += _format_sorry_section(added, removed, affected, issues)
    
    if style_report:
        summary += f"\n---\n\n<details><summary>🎨 **Style Guide Adherence**</summary>\n\n{style_report}\n</details>\n"
    
    if per_file_summaries:
        summary += f"\n---\n\n<details><summary>📄 **Per-File Summaries**</summary>\n\n" + "".join(f"*   {s}\n" for s in per_file_summaries) + "</details>\n"
    
    summary += f"\n---\n\n*Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.*"
    return summary

def find_sorry_issues(repo: Repository):
    """Finds all open issues with the 'proof wanted' label."""
    try:
        return list(repo.get_issues(state="open", labels=["proof wanted"]))
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

def find_existing_comment(pr: PullRequest):
    """Finds a comment previously posted by this action."""
    comment_regex = re.compile(COMMENT_IDENTIFIER.replace("{{timestamp}}", ".*?"))
    return next((c for c in pr.get_issue_comments() if comment_regex.search(c.body)), None)

def post_github_comment(pr: PullRequest, summary: str):
    """Finds and updates an existing comment or creates a new one."""
    existing_comment = find_existing_comment(pr)
    if existing_comment:
        existing_comment.edit(summary)
        print("Updated existing comment.")
    else:
        pr.create_issue_comment(summary)
        print("Created a new comment.")

# --- Main Execution ---
def main():
    """Main execution block."""
    # Ensure client can be initialized (checks API key)
    get_client()

    model_name = os.environ.get("INPUT_GEMINI_MODEL", 'gemini-3-flash-preview')
    keywords = [k.strip() for k in os.environ.get("INPUT_LEAN_KEYWORDS", 'def,abbrev,example,theorem,opaque,lemma,instance,constant,axiom').split(',')]
    style_guide_path = os.environ.get("INPUT_STYLE_GUIDE_PATH")

    try:
        with open("pr.diff", "r") as f:
            diff = f.read()
    except FileNotFoundError:
        sys.exit("Error: pr.diff not found.")

    analyzer = DiffAnalyzer(keywords)
    stats, added, removed, affected, added_decls, removed_decls, affected_decls = analyzer.analyze(diff)

    repo, pr, issues, pr_title, pr_body = None, None, [], "", ""
    if "GITHUB_TOKEN" in os.environ:
        try:
            repo, pr = get_github_objects(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], int(os.environ["PR_NUMBER"]))
            issues, pr_title, pr_body = find_sorry_issues(repo), pr.title, pr.body or ""
        except Exception as e:
            print(f"Warning: Could not fetch GitHub info: {e}")

    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated: diff = diff[:MAX_DIFF_CHARS]

    style_guide_content = ""
    if style_guide_path:
        try:
            with open(style_guide_path, "r") as f:
                style_guide_content = f.read()
        except FileNotFoundError:
            print(f"Warning: Style guide file not found at {style_guide_path}")

    diff_by_file = split_diff_into_files(diff)
    all_files = list(diff_by_file.keys())
    files_to_summarize = triage_files(all_files, diff_by_file, model_name) if all_files else []
    print(f"Triage agent selected {len(files_to_summarize)}/{len(all_files)} files to summarize.")

    per_file_summaries = []
    cache = SummaryCache(pr) if pr else None
    style_report = None
    
    summarize_template = _read_prompt_template("summarize_file.md")
    style_template = _read_prompt_template("check_style.md") if style_guide_content else ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        style_future = None
        if style_guide_content:
            style_future = executor.submit(check_style_adherence, diff, style_guide_content, model_name, style_template)

        future_to_file = {}
        for fp in files_to_summarize:
            fd = diff_by_file[fp]
            file_diff_hash = hashlib.sha256(fd.encode()).hexdigest()
            summary = cache.get(fp, file_diff_hash) if cache else None
            
            if summary:
                print(f"Cache hit for {fp}")
                per_file_summaries.append(f"**{fp}**: {summary}")
            else:
                print(f"Cache miss for {fp}. Queuing summarization.")
                future = executor.submit(summarize_file_diff, fp, fd, model_name, summarize_template)
                future_to_file[future] = (fp, file_diff_hash)

        for future in concurrent.futures.as_completed(future_to_file):
            fp, file_diff_hash = future_to_file[future]
            try:
                res = future.result()
                if res:
                    summary = res.strip()
                    if cache:
                        cache.update(fp, file_diff_hash, summary)
                    per_file_summaries.append(f"**{fp}**: {summary}")
                else:
                    print(f"Warning: Summarization for {fp} failed.")
            except Exception as exc:
                print(f"Warning: Summarization for {fp} generated an exception: {exc}")

        if style_future:
            try:
                style_report = style_future.result()
            except Exception as exc:
                print(f"Warning: Style check generated an exception: {exc}")

    try:
        draft_summary = synthesize_summary(per_file_summaries, model_name, pr_title, pr_body)
    except Exception as e:
        print(f"Error synthesizing final summary: {e}")
        # Create a fallback summary if synthesis fails
        draft_summary = "Failed to generate AI summary. Please check the per-file summaries and statistics below."

    try:
        ai_summary = refine_summary(draft_summary, pr_title, pr_body, model_name)
    except Exception as e:
        print(f"Warning: Refiner agent failed. Using draft summary. {e}")
        ai_summary = draft_summary

    final_summary = format_summary(ai_summary, stats, added, removed, affected, added_decls, removed_decls, affected_decls, truncated, issues, per_file_summaries, style_report, cache)
    
    if pr:
        post_github_comment(pr, final_summary)
    else:
        print("Not in a GitHub Actions context. Printing summary instead:\n", final_summary)

if __name__ == "__main__":
    main()