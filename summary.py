import os
import re
import sys
import json
import hashlib
import concurrent.futures
import threading
import time
from datetime import datetime
from collections import defaultdict
from google import genai
from github import Github, Auth
from github.PullRequest import PullRequest
from github.Repository import Repository

# --- Constants ---
MAX_DIFF_CHARS = 1_500_000
LARGE_PR_FILE_THRESHOLD = 50  # Files to summarize above which tiered mode activates
LARGE_PR_SYNTHESIS_THRESHOLD = 40  # Per-file summaries above which two-stage synthesis activates
COMMENT_IDENTIFIER = "<!-- gemini-pr-summary-{{timestamp}} -->"
CACHE_IDENTIFIER = "<!-- gemini-cache: "

# --- Global Client and Rate Limiter ---
_client = None
_api_semaphore = threading.Semaphore(5)  # Cap concurrent Gemini API calls

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
    """A helper function to call the Gemini API with retry logic and rate limiting."""
    client = get_client()
    kwargs = {}
    if response_mime_type:
        kwargs["config"] = {"response_mime_type": response_mime_type}

    with _api_semaphore:
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

def synthesize_summary(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint=""):
    """Synthesizes a final summary from per-file summaries (Reduce step)."""
    summaries_text = "\n".join(f"- {s}" for s in per_file_summaries)
    prompt_template = _read_prompt_template("synthesize_summary.md")
    prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                            .replace("{{PR_BODY}}", pr_body) \
                            .replace("{{PER_FILE_SUMMARIES}}", summaries_text) \
                            .replace("{{PR_TYPE_HINT}}", pr_type_hint)
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

_PROOF_RELEVANT_PATTERNS = re.compile(r'\b(sorry|admit|native_decide)\b')

def _detect_proof_signals(file_diff):
    """Check if a file diff contains proof-relevant keywords in added/removed lines."""
    signals = set()
    for line in file_diff.splitlines():
        if not line.startswith(('+', '-')) or line.startswith(('+++', '---')):
            continue
        if _PROOF_RELEVANT_PATTERNS.search(line):
            signals.update(m.group() for m in _PROOF_RELEVANT_PATTERNS.finditer(line))
    return signals

def _build_file_list_str(file_paths, diff_by_file, annotate_signals=False):
    """Build a formatted file list with line counts for triage prompts.
    If annotate_signals is True, appends proof-relevant signal tags."""
    file_list_with_counts = []
    for fp in file_paths:
        diff = diff_by_file[fp]
        added = sum(1 for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
        removed = sum(1 for line in diff.splitlines() if line.startswith('-') and not line.startswith('---'))
        entry = f"{fp} (+{added}/-{removed})"
        if annotate_signals:
            signals = _detect_proof_signals(diff)
            if signals:
                entry += f" [contains: {', '.join(sorted(signals))}]"
        file_list_with_counts.append(entry)
    return "\n".join(file_list_with_counts)

def _clean_json_response(response):
    """Strip markdown code block formatting from a JSON response."""
    clean = response.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) > 2:
            clean = "\n".join(lines[1:-1])
        else:
            clean = clean.strip("`").removeprefix("json").strip()
    return clean

def triage_files(file_paths, diff_by_file, model_name):
    """Uses the AI to filter out noise files before summarization.
    For large PRs, returns (high_priority, low_priority) tuple.
    For normal PRs, returns (files_to_summarize, []) tuple."""
    if not file_paths:
        return [], []

    use_tiered = len(file_paths) > LARGE_PR_FILE_THRESHOLD
    file_list_str = _build_file_list_str(file_paths, diff_by_file, annotate_signals=use_tiered)

    if use_tiered:
        prompt_template = _read_prompt_template("triage_tiered.md")
    else:
        prompt_template = _read_prompt_template("triage.md")
    prompt = prompt_template.replace("{{FILE_LIST}}", file_list_str)

    response = _call_gemini(prompt, model_name, response_mime_type="application/json")
    if not response:
        print("Warning: Triage agent failed. Proceeding with all files.")
        return file_paths, []
    try:
        clean_response = _clean_json_response(response)
        parsed = json.loads(clean_response)

        if use_tiered and isinstance(parsed, dict):
            high_set = set(parsed.get("high", []))
            low_set = set(parsed.get("low", []))
            # Force-promote any low-priority file that has proof-relevant signals
            for fp in list(low_set):
                if fp in diff_by_file and _detect_proof_signals(diff_by_file[fp]):
                    print(f"Promoting {fp} to high priority (contains proof-relevant signals).")
                    low_set.discard(fp)
                    high_set.add(fp)
            # Preserve original file order from file_paths
            high = [f for f in file_paths if f in high_set]
            low = [f for f in file_paths if f in low_set]
            return high, low
        elif isinstance(parsed, list):
            return [f for f in parsed if f in file_paths], []
        return file_paths, []
    except json.JSONDecodeError:
        print(f"Warning: Triage agent returned invalid JSON: {response}. Proceeding with all files.")
        return file_paths, []

def synthesize_summary_staged(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint=""):
    """Two-stage synthesis for large PRs: group by directory, synthesize groups, then global."""
    # Group summaries by top-level directory
    groups = defaultdict(list)
    for s in per_file_summaries:
        # Extract file path from "**path/to/file**: summary"
        match = re.match(r'\*\*([^*]+)\*\*:', s)
        if match:
            path = match.group(1)
            parts = path.split('/')
            group_key = parts[0] if len(parts) > 1 else "root"
        else:
            group_key = "other"
        groups[group_key].append(s)

    # Synthesize each directory group
    group_summaries = []
    for group_key, summaries in sorted(groups.items()):
        if len(summaries) <= 3:
            # Small groups don't need their own synthesis
            group_summaries.extend(summaries)
        else:
            group_text = "\n".join(f"- {s}" for s in summaries)
            prompt_template = _read_prompt_template("synthesize_summary.md")
            prompt = prompt_template.replace("{{PR_TITLE}}", pr_title) \
                                    .replace("{{PR_BODY}}", "") \
                                    .replace("{{PER_FILE_SUMMARIES}}", group_text) \
                                    .replace("{{PR_TYPE_HINT}}", f"This is a sub-summary for the `{group_key}/` directory. ")
            result = _call_gemini(prompt, model_name)
            if result:
                group_summaries.append(f"**{group_key}/**: {result.strip()}")
            else:
                group_summaries.extend(summaries)

    # Final global synthesis
    return synthesize_summary(group_summaries, model_name, pr_title, pr_body, pr_type_hint)

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
        self.warnings = []  # Lean quality signal warnings
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
        return stats, self.added_sorries, self.removed_sorries, self.affected_sorries, self.added_decls, self.removed_decls, self.affected_decls, self.warnings

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

    # Patterns for Lean quality signals (only checked on added lines)
    _QUALITY_SIGNALS = [
        (re.compile(r'\badmit\b'), "admit", "`admit` bypasses proof checking"),
        (re.compile(r'\bnative_decide\b'), "native_decide", "`native_decide` bypasses the kernel — potential soundness concern"),
        (re.compile(r'^\s*#check\b'), "#check", "`#check` debug command left in code"),
        (re.compile(r'^\s*#eval\b'), "#eval", "`#eval` debug command left in code"),
        (re.compile(r'set_option\s+autoImplicit\s+true'), "autoImplicit", "`set_option autoImplicit true` re-enabled"),
    ]

    def _process_line(self, line):
        self._track_sorries_and_decls(line)
        if line.startswith('+'):
            self._check_quality_signals(line)
            self._new_line_num += 1
        elif line.startswith('-'):
            self._old_line_num += 1
        else:
            self._old_line_num += 1
            self._new_line_num += 1

    def _check_quality_signals(self, line):
        content = line[1:]  # Strip the '+' prefix
        # Skip if inside a comment
        comment_match = re.search(r'(?:^|\s)--', content)
        for pattern, name, message in self._QUALITY_SIGNALS:
            match = pattern.search(content)
            if match:
                # If it's after a comment marker, skip
                if comment_match and match.start() > comment_match.start():
                    continue
                self.warnings.append({
                    'signal': name,
                    'message': message,
                    'file': self._current_file,
                    'line': self._new_line_num,
                })

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
def _compute_config_fingerprint(model_name, prompt_template):
    """Hash the model name and prompt template so cache invalidates when either changes."""
    content = f"{model_name}\n{prompt_template}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]

class SummaryCache:
    """Handles caching of file diff summaries. Thread-safe."""
    def __init__(self, pr: PullRequest, config_fingerprint: str):
        self._lock = threading.Lock()
        self._config_fingerprint = config_fingerprint
        self._cache = self._load_from_comment(pr)

    def _load_from_comment(self, pr: PullRequest):
        comment = find_existing_comment(pr)
        if comment and CACHE_IDENTIFIER in comment.body:
            try:
                cache_str = comment.body.split(CACHE_IDENTIFIER, 1)[1].split("-->", 1)[0]
                data = json.loads(cache_str)
                # Invalidate entire cache if config fingerprint changed
                if data.get("_config") != self._config_fingerprint:
                    print("Cache invalidated: model or prompt template changed.")
                    return {}
                return data
            except (IndexError, json.JSONDecodeError):
                return {}
        return {}

    def get(self, file_path, file_diff_hash):
        with self._lock:
            if file_path in self._cache and isinstance(self._cache[file_path], dict) \
                    and self._cache[file_path].get('hash') == file_diff_hash:
                return self._cache[file_path]['summary']
            return None

    def update(self, file_path, file_diff_hash, summary):
        with self._lock:
            self._cache[file_path] = {'hash': file_diff_hash, 'summary': summary}

    def to_json(self):
        with self._lock:
            data = dict(self._cache)
            data["_config"] = self._config_fingerprint
            return json.dumps(data)

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

def _find_related_issue(sorry_info, issues):
    """Find an issue related to a sorry entry by tracker ID, file path, or declaration name."""
    sid = sorry_info['id']
    file_path = sorry_info['file']
    # Extract the declaration name from the id (format: "name@file")
    decl_name = sid.split('@')[0] if '@' in sid else ""

    for issue in issues:
        if not issue.body:
            continue
        # Exact tracker ID match (strongest signal)
        if f"<!-- sorry-tracker-id: {sid} -->" in issue.body:
            return issue
        # Match on both file path and declaration name (good signal)
        if decl_name and decl_name in issue.body and file_path in issue.body:
            return issue
    return None

def _format_sorry_section(added, removed, affected, issues):
    res = "\n---\n\n**`sorry` Tracking**\n\n"
    if removed:
        res += f"<details><summary>✅ **Removed:** {len(removed)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in removed) + "</details>\n"
    if added:
        res += f"<details><summary>❌ **Added:** {len(added)} `sorry`(s)</summary>\n\n" + "".join(f"*   {s}\n" for s in added) + "</details>\n"
    if affected:
        res += f"<details><summary>✏️ **Affected:** {len(affected)} `sorry`(s) (line number changed)</summary>\n\n"
        for s in affected:
            related_issue = _find_related_issue(s, issues)
            issue_link = f" (Issue #{related_issue.number})" if related_issue else ""
            res += f"*   `{s['context']}` in `{s['file']}` moved from L{s['old_line']} to L{s['new_line']}{issue_link}\n"
        res += "</details>\n"
    if not any([added, removed, affected]):
        res += "*   No `sorry`s were added, removed, or affected.\n"
    return res

def _format_warnings_section(warnings):
    """Format Lean quality signal warnings."""
    if not warnings:
        return ""
    res = "\n---\n\n**Lean Quality Signals**\n\n"
    # Group by signal type
    by_signal = defaultdict(list)
    for w in warnings:
        by_signal[w['signal']].append(w)
    for signal, items in by_signal.items():
        message = items[0]['message']
        if len(items) == 1:
            w = items[0]
            res += f"*   ⚠️ {message} in `{w['file']}` (L{w['line']})\n"
        else:
            locations = ", ".join(f"`{w['file']}` L{w['line']}" for w in items)
            res += f"*   ⚠️ {message} — {len(items)} occurrence(s): {locations}\n"
    return res

def _format_sorry_delta(added, removed):
    """Format a top-level sorry delta status line."""
    n_added = len(added)
    n_removed = len(removed)
    delta = n_added - n_removed
    if n_added == 0 and n_removed == 0:
        return ""
    parts = []
    if n_removed:
        parts.append(f"{n_removed} removed")
    if n_added:
        parts.append(f"{n_added} added")
    detail = ", ".join(parts)
    if delta < 0:
        return f"> **`sorry` delta: {delta}** ({detail}) — net proof progress\n\n"
    elif delta > 0:
        return f"> **`sorry` delta: +{delta}** ({detail}) — proof obligations increased\n\n"
    else:
        return f"> **`sorry` delta: 0** ({detail}) — no net change\n\n"

def format_summary(ai_summary, stats, added, removed, affected, added_decls, removed_decls, affected_decls, truncated, issues, per_file_summaries, style_report, cache, warnings=None, title_note="", upstream_note=""):
    """Formats the final summary comment in Markdown."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S")
    comment_id = COMMENT_IDENTIFIER.replace("{{timestamp}}", timestamp)
    cache_html = f"{CACHE_IDENTIFIER}{cache.to_json()}-->\n\n" if cache else ""

    sorry_delta = _format_sorry_delta(added, removed)
    summary = f"### 🤖 Gemini PR Summary\n\n{comment_id}\n\n{cache_html}{title_note}{upstream_note}{sorry_delta}{ai_summary}\n"

    if truncated:
        summary += "> *Note: The diff was too large and was truncated.*\n"
    
    summary += _format_stats_section(stats)
    summary += _format_decls_section(added_decls, removed_decls, affected_decls)
    summary += _format_sorry_section(added, removed, affected, issues)

    if warnings:
        summary += _format_warnings_section(warnings)

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

# --- PR Title Validation ---
_CONVENTIONAL_COMMIT_RE = re.compile(
    r'^(?P<type>feat|fix|doc|docs|style|refactor|chore|ci|test|perf|build|revert)'
    r'(?:\((?P<scope>[^)]+)\))?:\s+(?P<subject>.+)$'
)

def validate_pr_title(title):
    """Validate PR title against conventional commit format.
    Returns (is_valid, parsed_type, message)."""
    if not title:
        return True, None, None  # No title to validate
    match = _CONVENTIONAL_COMMIT_RE.match(title)
    if match:
        return True, match.group('type'), None
    return False, None, f"PR title does not follow conventional commit format `type[(scope)]: subject`. Got: `{title}`"

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
    validate_title = os.environ.get("INPUT_VALIDATE_TITLE", "false").lower() == "true"
    upstream_path = os.environ.get("INPUT_UPSTREAM_PATH", "")

    try:
        with open("pr.diff", "r") as f:
            diff = f.read()
    except FileNotFoundError:
        sys.exit("Error: pr.diff not found.")

    analyzer = DiffAnalyzer(keywords)
    stats, added, removed, affected, added_decls, removed_decls, affected_decls, warnings = analyzer.analyze(diff)

    repo, pr, issues, pr_title, pr_body = None, None, [], "", ""
    if "GITHUB_TOKEN" in os.environ:
        try:
            repo, pr = get_github_objects(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], int(os.environ["PR_NUMBER"]))
            issues, pr_title, pr_body = find_sorry_issues(repo), pr.title, pr.body or ""
        except Exception as e:
            sys.exit(f"Error: Could not initialize GitHub API — cannot post PR comment: {e}")

    # Title validation and upstream path detection
    title_note = ""
    pr_type_hint = ""
    if validate_title and pr_title:
        is_valid, parsed_type, message = validate_pr_title(pr_title)
        if not is_valid:
            title_note = f"> ⚠️ {message}\n\n"
        elif parsed_type:
            pr_type_hint = f"The PR type is `{parsed_type}`. "

    upstream_note = ""
    if upstream_path:
        upstream_files = [f for f in analyzer.files_changed if f.startswith(upstream_path)]
        if upstream_files:
            upstream_note = f"> ℹ️ This PR modifies {len(upstream_files)} file(s) under `{upstream_path}` — consider whether a corresponding upstream PR is needed.\n\n"

    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        # Truncate at file boundaries to avoid malformed diffs
        cut_point = diff.rfind("\ndiff --git ", 0, MAX_DIFF_CHARS)
        if cut_point > 0:
            diff = diff[:cut_point]
        else:
            diff = diff[:MAX_DIFF_CHARS]

    style_guide_content = ""
    if style_guide_path:
        try:
            with open(style_guide_path, "r") as f:
                style_guide_content = f.read()
        except FileNotFoundError:
            print(f"Warning: Style guide file not found at {style_guide_path}")

    diff_by_file = split_diff_into_files(diff)
    all_files = list(diff_by_file.keys())
    high_priority, low_priority = triage_files(all_files, diff_by_file, model_name) if all_files else ([], [])
    files_to_summarize = high_priority
    if low_priority:
        print(f"Triage agent selected {len(high_priority)} high-priority, {len(low_priority)} low-priority, skipped {len(all_files) - len(high_priority) - len(low_priority)} files.")
    else:
        print(f"Triage agent selected {len(files_to_summarize)}/{len(all_files)} files to summarize.")

    per_file_summaries = []
    style_report = None

    summarize_template = _read_prompt_template("summarize_file.md")
    config_fp = _compute_config_fingerprint(model_name, summarize_template)
    cache = SummaryCache(pr, config_fp) if pr else None
    style_template = _read_prompt_template("check_style.md") if style_guide_content else ""

    # Collect summaries keyed by file path so we can assemble in deterministic order
    summary_by_file = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        style_future = None
        if style_guide_content:
            style_future = executor.submit(check_style_adherence, diff, style_guide_content, model_name, style_template)

        future_to_file = {}
        for fp in files_to_summarize:
            fd = diff_by_file[fp]
            file_diff_hash = hashlib.sha256(fd.encode()).hexdigest()
            cached_summary = cache.get(fp, file_diff_hash) if cache else None

            if cached_summary:
                print(f"Cache hit for {fp}")
                summary_by_file[fp] = cached_summary
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
                    summary_by_file[fp] = summary
                else:
                    print(f"Warning: Summarization for {fp} returned no result.")
                    summary_by_file[fp] = "*Summary unavailable — AI generation failed after retries.*"
            except Exception as exc:
                print(f"Warning: Summarization for {fp} generated an exception: {exc}")
                summary_by_file[fp] = f"*Summary unavailable — error: {exc}*"

        if style_future:
            try:
                style_report = style_future.result()
            except Exception as exc:
                print(f"Warning: Style check generated an exception: {exc}")

    # Assemble per-file summaries in original file order for deterministic output
    for fp in files_to_summarize:
        if fp in summary_by_file:
            per_file_summaries.append(f"**{fp}**: {summary_by_file[fp]}")

    # Add low-priority files as brief mentions (no AI call)
    for fp in low_priority:
        fd = diff_by_file[fp]
        added_count = sum(1 for line in fd.splitlines() if line.startswith('+') and not line.startswith('+++'))
        removed_count = sum(1 for line in fd.splitlines() if line.startswith('-') and not line.startswith('---'))
        per_file_summaries.append(f"**{fp}**: *(minor changes, +{added_count}/-{removed_count})*")

    try:
        # Use two-stage synthesis for very large PRs
        if len(per_file_summaries) > LARGE_PR_SYNTHESIS_THRESHOLD:
            print(f"Large PR detected ({len(per_file_summaries)} summaries). Using two-stage synthesis.")
            draft_summary = synthesize_summary_staged(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint)
        else:
            draft_summary = synthesize_summary(per_file_summaries, model_name, pr_title, pr_body, pr_type_hint)
    except Exception as e:
        print(f"Error synthesizing final summary: {e}")
        # Create a fallback summary if synthesis fails
        draft_summary = "Failed to generate AI summary. Please check the per-file summaries and statistics below."

    try:
        ai_summary = refine_summary(draft_summary, pr_title, pr_body, model_name)
    except Exception as e:
        print(f"Warning: Refiner agent failed. Using draft summary. {e}")
        ai_summary = draft_summary

    final_summary = format_summary(ai_summary, stats, added, removed, affected, added_decls, removed_decls, affected_decls, truncated, issues, per_file_summaries, style_report, cache, warnings, title_note, upstream_note)
    
    if pr:
        post_github_comment(pr, final_summary)
    elif "GITHUB_TOKEN" in os.environ:
        sys.exit("Error: GitHub PR object is unavailable — cannot post comment.")
    else:
        print("No GITHUB_TOKEN set. Printing summary to stdout:\n", final_summary)

if __name__ == "__main__":
    main()