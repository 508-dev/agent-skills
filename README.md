# 508.dev Agent Skills

Public, portable agent skills maintained by [508.dev](https://508.dev). Each
skill lives below `skills/`, carries its own `SKILL.md` instructions, and can be
installed independently.

## Use with Any Agent Runtime

The skill directory is framework-neutral. An agent runtime needs to give its
agent access to the skill folder, load `SKILL.md` when the skill applies, and
allow the bundled launcher to run through a terminal. The social-media skill
requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/); image analysis is
only needed when an agent chooses to inspect downloaded media.

If your runtime has a skill-discovery directory, copy or symlink the skill
there. Otherwise, provide the agent with the absolute skill directory and tell
it to follow the directory's `SKILL.md` for matching requests.

```sh
git clone https://github.com/508-dev/agent-skills.git
cp -R agent-skills/skills/social-media-extract <agent-skill-dir>/
```

Reload the agent's skills after installing. The optional
`agents/openai.yaml` file supplies OpenAI UI metadata only; other runtimes can
ignore it.

## Install in Codex

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
use installs the reviewed, locked Python runtime; later uses reuse it.

```sh
SKILL_DIR="<agent-skill-dir>/social-media-extract"
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
