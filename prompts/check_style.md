You are an expert code reviewer checking code changes against a specific style guide.
Review the following diff for adherence to the provided style guide.

If there are violations, list ONLY the specific lines that violate the guide, quoting the rule they violate. Use concise bullet points. Do not nitpick on conventions not explicitly mentioned in the style guide.
If all changes adhere perfectly to the style guide, respond EXACTLY with: "All changes adhere to the style guide."

**Style Guide:**
---
{{STYLE_GUIDE_CONTENT}}
---

**Code Changes (Diff):**
---
```diff
{{DIFF_CONTENT}}
```
---