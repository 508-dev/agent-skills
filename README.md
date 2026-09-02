# 508.dev Agent Skills

Public, installable Codex skills maintained by [508.dev](https://508.dev).
Each skill lives below `skills/` and can be installed independently.

## Install a Skill in Codex

Install `social-media-extract` with Codex's bundled GitHub installer:

```sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo 508-dev/agent-skills \
  --path skills/social-media-extract
```

Start a new Codex turn after installing so it discovers the skill.

## Included Skills

| Skill | Use it for |
| --- | --- |
| [`social-media-extract`](skills/social-media-extract/) | Extracting evidence-backed places from public Instagram posts/Reels and Facebook Reels, with Google Maps search links. |

## Social Media Extract

The skill handles public `instagram.com/p/...`, `instagram.com/reel/...`, and
`facebook.com/reel/...` URLs. Its launcher uses `uv run --locked`, so the first
use installs the reviewed, locked Python runtime; later uses reuse that runtime.

```sh
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/social-media-extract"
"$SKILL_DIR/scripts/social-media-extract" --json --scrape-only \
  'https://www.instagram.com/reel/EXAMPLE/'
```

It does not call Google Places or require a Google API key. It never requests
credentials, cookies, verification codes, or CAPTCHA solutions. A local,
owner-controlled CloakBrowser Manager can optionally supply an existing session
when public access is unavailable; it is not included or required for ordinary
public posts.

## Development

Run the dependency-free package checks with:

```sh
python3 -m unittest discover -s tests -v
```

When adding a skill, place a complete `SKILL.md` and its resources in
`skills/<skill-name>/` so it remains independently installable.

## License

[MIT](LICENSE)
