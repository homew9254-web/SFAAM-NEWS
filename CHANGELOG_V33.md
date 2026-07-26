# CHANGELOG V33 — Bug Fixes + Edit & Publish Feature

Date: 2026-07-23
Project: SFAAM NEWS PRO 1

---

## Summary

This patch fixes **3 bugs** and adds **1 new admin feature** ("Edit & Publish
in one click"). All fixes are covered by an automated integration test.

---

## BUG #1 (CRITICAL) — Admin could not edit draft articles

### Symptom
Clicking the **Edit** button on a draft article in the Admin panel threw the
error *"Could not load article"*. The Edit modal never opened for drafts,
pending_review, or rejected articles.

### Root cause
`openEditModal(id)` in `static/admin.html` fetched the article via the
**public** endpoint `GET /api/articles/{id}`. That endpoint filters out any
article whose `status` is not `"published"` and returns **404** for drafts:

```python
# main.py line 949 — BUG
if a.status is not None and a.status != "published":
    raise HTTPException(404, "Article not found")
```

So as soon as the admin clicked Edit on a draft, the fetch failed and the
modal couldn't load.

### Fix
Added a new admin-only endpoint `GET /api/admin/articles/{article_id}` in
`main.py` (around line 3208). It returns the article regardless of status
and includes every editable field (`ai_content`, `status`, `tldr_summary`,
`fact_check_status`, etc.). The `openEditModal()` function in
`static/admin.html` was switched to call this admin endpoint.

---

## BUG #2 — PATCH endpoint did not support changing the article status

### Symptom
The Edit modal could change title / summary / content / region / keywords /
image / TL;DR, but the admin **could not change the publication status**
from the Edit modal. To publish an edited draft, the admin had to close the
modal, find the article in the list, and click a separate Publish button.

### Fix
Added an optional `status` field to the `ArticleEditIn` Pydantic model in
`main.py` (around line 3193). Allowed values: `published | draft |
pending_review | rejected`. The `edit_article()` PATCH handler now writes
`status` to the article, invalidates the sitemap cache, and logs a
`article.publish_via_edit` audit event when the admin promotes a draft to
published.

---

## FEATURE — "Save & Publish" button in the Edit modal

### What changed
The Edit modal in `static/admin.html` (`openEditModal()` function around
line 1282) was redesigned:

- Added a **Status dropdown** showing all four publication states, with the
  current status pre-selected.
- Added a yellow **status hint banner** when the article is not yet
  published, explaining how to publish.
- Replaced the single **Save Changes** button with three actions:
  - **Cancel** — closes the modal without saving.
  - **Save Changes** — saves the edits, keeps the status as selected in the
    dropdown.
  - **Save & Publish** — saves the edits AND forces `status='published'` so
    the article goes live immediately.
- After saving, the modal now also refreshes the article list **and** the
  stats panel (so the "Total Articles" / "Published Today" counters update).
- Added a basic length-validation toast for the title and content fields.

This means the admin can now click **Edit** on any draft, change the title
/ summary / content / region / keywords / image / TL;DR, and click
**Save & Publish** to push it live in a single action — exactly as
requested.

---

## BUG #3 — Numbered lists in articles rendered as raw `<oli>` tags

### Symptom
Markdown numbered lists like:

```markdown
1. First item
2. Second item
3. Third item
```

…rendered on the public article page as raw text `<oli data-num="1">First
item</oli>` instead of an actual `<ol><li>…` list.

### Root cause
The `mdToHtml()` function in `static/article.html` (around line 1388) had a
two-step regex:

```js
// step 1 — produces <oli data-num="1">…</oli>  (note: WITH attribute)
.replace(/^(\d+)\.\s+(.+)$/gm, '<oli data-num="$1">$2</oli>')
// step 2 — expects literal <oli>  (no attributes!) — DOESN'T MATCH
.replace(/(<oli>.*<\/oli>\n?)+/gs, m => '<ol>' + … + '</ol>')
```

The first regex produces `<oli data-num="1">…</oli>` (with an attribute),
but the second regex matched only the literal tag `<oli>` (no attributes
allowed). The two regexes never agreed, so numbered lists were never
wrapped in `<ol>`.

### Fix
Changed the second regex to `<oli[^>]*>` so it matches an `<oli>` tag with
any attributes:

```js
.replace(/(<oli[^>]*>.*<\/oli>\n?)+/gs, m => '<ol>' + … + '</ol>')
```

---

## Files Changed

| File | Change |
|------|--------|
| `main.py` | + new endpoint `GET /api/admin/articles/{article_id}`; + `status` field on `ArticleEditIn`; + audit log for status changes via edit; + `article_status` in PATCH response. |
| `static/admin.html` | Rewrote `openEditModal()` to use the admin endpoint, added Status dropdown, status hint banner, and **Save & Publish** button. |
| `static/article.html` | Fixed `mdToHtml()` numbered-list regex (`<oli>` → `<oli[^>]*>`). |

---

## Verification

Run the integration test:

```bash
python3 /home/z/my-project/scripts/test_fixes.py
```

All 6 test steps pass:

1. Admin login works.
2. Create article → demote to draft via PATCH.
3. Public GET 404s on draft; admin GET returns the draft.
4. PATCH with `status='published'` + new title/summary/content succeeds.
5. Public GET now returns the published article with the new title.
6. mdToHtml regex correctly wraps numbered lists in `<ol><li>`.
