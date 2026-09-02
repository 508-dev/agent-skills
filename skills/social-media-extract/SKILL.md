---
name: social-media-extract
description: Extract places from public Instagram posts/Reels and Facebook Reels when a user provides a supported post URL; not for profiles, feeds, or private content.
license: MIT
---

# Social Media Extract

Use this skill when the user supplies a public Instagram post/Reel or Facebook
Reel and wants the places, venues, or locations mentioned in it. Do not use it
for profiles, Stories, feeds, private messages, or broad place discovery.

This folder is framework-neutral: an agent only needs to load this `SKILL.md`,
resolve the bundled launcher relative to the folder, and have terminal access.
`agents/openai.yaml` is optional OpenAI UI metadata and does not affect the
extractor's runtime behavior.

## Run the Extractor

Resolve the directory that contains this `SKILL.md`, then invoke the bundled
`scripts/social-media-extract` launcher rather than the Python file directly.
Start with structured, scrape-only output so no separately configured vision
endpoint is invoked implicitly:

```sh
<skill-dir>/scripts/social-media-extract --json --scrape-only '<post-or-reel-url>'
```

It accepts a mix of supported URLs in one command. Treat the JSON as the source
of truth. Use `post.caption`, `post.location`, diagnostics, and any returned
`places[].maps_url`. A Google Maps search URL is not proof of a unique venue
match.

If visual context is materially needed, rerun with `--download-media`. Inspect
only the returned local paths using the available image tools, and do not claim
places that are not supported by the caption, tagged location, or visible
evidence. The download writes bounded media to an owner-only local cache.

## Handle Results

- `scraped` means metadata was obtained without automatic vision analysis.
- `ok` includes places extracted through an explicitly configured vision
  endpoint; preserve the returned Maps URLs unchanged.
- `restricted`, `scrape_failed`, and `login_required` mean the source could not
  be read normally. State the diagnostic plainly; do not bypass restrictions,
  substitute an unrelated web search, or invent locations.

Reply with a compact Markdown list of evidence-backed places and their returned
Google Maps search links. When the source only supports a tagged location or
caption-derived candidate, say so.

## Optional Owner-Controlled Login

The public fetch path is the default. `--login` is only for owners who already
run a compatible, local-loopback CloakBrowser Manager and deliberately choose
to use a persistent browser profile. The owner completes all login and
site-owned verification in that browser.

Never ask for or handle passwords, cookies, session exports, CAPTCHA answers,
2FA codes, recovery codes, or browser-manager tokens in chat. Do not use Google
Places or a Google API key.
