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

### Ask Your Agent to Install It

Paste this into an agent that can manage its own skills:

```text
Install the `social-media-extract` skill from https://github.com/508-dev/agent-skills.

Use your normal skill-installation mechanism if you have one. Otherwise, clone
or download the repository and install or link `skills/social-media-extract`
in your configured user-level skill directory. Preserve the bundled executable
scripts, load its `SKILL.md` for public Instagram post/Reel and Facebook
post/Reel or share-link requests, and use `scripts/social-media-extract` as
the launcher. The runtime needs Python 3.10+ and uv.

Never ask me for passwords, cookies, session exports, 2FA codes, or CAPTCHA
solutions. Tell me the installed path and whether I need to restart or reload
you.
```

For a manual installation, replace the example path below with your runtime's
configured skill-discovery directory:

```sh
# Set this to your agent runtime's skill-discovery directory.
AGENT_SKILLS_DIR=/path/to/your/agent/skills
git clone https://github.com/508-dev/agent-skills.git
mkdir -p "$AGENT_SKILLS_DIR"
cp -R agent-skills/skills/social-media-extract "$AGENT_SKILLS_DIR/"
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
| [`social-media-extract`](skills/social-media-extract/) | Extracting evidence-backed places from public Instagram posts/Reels and Facebook posts/Reels or share links, with Google Maps search links. |

## Social Media Extract

The skill handles public `instagram.com/p/...`, `instagram.com/reel/...`,
`facebook.com/reel/...`, `facebook.com/<profile>/posts/...`, and
`facebook.com/share/...` URLs. A share link must resolve to public content. Its
launcher uses `uv run --locked`, so the first use installs the reviewed, locked
Python runtime; subsequent uses reuse it.

```sh
SKILL_DIR="${AGENT_SKILLS_DIR}/social-media-extract"
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
