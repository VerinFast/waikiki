"""Generate images for an article with a local image CLI (agy / gemini).

Two steps, mirroring how the user works by hand:

1. **Claude writes the prompt.** The `claude` CLI turns the article context + a
   short human description into one vivid image-generation prompt.
2. **agy/gemini renders it.** The image CLI runs headless (`--print`), told to
   use its built-in image tool and save a PNG into the article's own folder.
   That folder is passed with `--add-dir` so the CLI can see earlier images and
   keep the style consistent across one article.

The PNG is then ingested into the wiki (so it displays and exports like any
uploaded image) while the original file stays in the article folder for the next
generation's `--add-dir` context.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config, db, render, shellenv, store


def article_image_dir(wiki: str, slug: str) -> Path:
    """Per-article image folder — one place per article, for --add-dir consistency."""
    d = config.DATA_DIR / "images" / wiki / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_filename(folder: Path, description: str) -> str:
    base = render.slugify(description)[:40] or "image"
    n = len(list(folder.glob("*.png"))) + 1
    return f"{base}-{n}.png"


def write_image_prompt(title: str, article_md: str, description: str,
                       model: str = "") -> str:
    """Ask Claude to craft a vivid image prompt. Falls back to the raw
    description if the claude CLI isn't available or errors."""
    claude = shellenv.which("claude")
    if not claude:
        return description
    excerpt = (article_md or "")[:1500]
    ask = (
        "You write a single vivid prompt for an AI image generator. Output ONLY "
        "the prompt text — no preamble, no quotes, no markdown.\n\n"
        f"Wiki article: {title}\n\n{excerpt}\n\n"
        f"The user wants an illustration described as: {description}\n\n"
        "Write one detailed image-generation prompt (subject, composition, style, "
        "palette, mood) that suits this article. Under 100 words."
    )
    argv = [claude, "-p", ask] + (["--model", model] if model else [])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                              env=shellenv.env())
    except Exception:
        return description
    out = re.sub(r"\x1b\[[0-9;]*m", "", (proc.stdout or "").strip())
    return out or description


def generate(slug: str, description: str, image_cli: str = "agy",
             model: str = "", timeout: int = 300) -> dict:
    """Generate an image for page `slug` from a short description. Synchronous
    (spawns CLIs) — call in a worker thread from async code."""
    description = (description or "").strip()
    if not description:
        return {"ok": False, "error": "Describe the image first."}
    page = store.get_page(slug)
    if not page:
        return {"ok": False, "error": f"No page '{slug}'."}

    cli = shellenv.which(image_cli)
    if not cli:
        return {"ok": False,
                "error": f"`{image_cli}` CLI not found. Install it, or set a "
                         f"different image CLI in Settings → Images."}

    wiki = db.active_wiki()
    folder = article_image_dir(wiki, slug)
    img_prompt = write_image_prompt(page["title"], page["markdown"], description)

    save_path = folder / _next_filename(folder, description)
    instruction = (
        f"{img_prompt}\n\n"
        f"Render at 1024x1024 px. Use your built-in image generation tool and save "
        f"the result as a PNG to {save_path}. If {folder} already contains images, "
        f"keep the visual style consistent with them."
    )
    argv = [cli]
    if model:
        argv += ["-m", model]
    argv += ["--add-dir", str(folder), "--dangerously-skip-permissions",
             "--print", instruction]

    before = set(folder.glob("*.png"))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=shellenv.env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Image generation timed out after {timeout}s."}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to run {image_cli}: {exc}"}

    # Prefer the exact path; otherwise take the newest PNG that appeared.
    img_file = save_path if save_path.exists() else None
    if img_file is None:
        fresh = sorted(set(folder.glob("*.png")) - before,
                       key=lambda p: p.stat().st_mtime)
        img_file = fresh[-1] if fresh else None
    if img_file is None:
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {"ok": False,
                "error": f"{image_cli} didn't produce a PNG. Output: {out[:400]}"}

    data = img_file.read_bytes()
    image_id = store.save_image(img_file.name, "image/png", data)
    url = f"/image/{image_id}/{img_file.name}"
    return {"ok": True, "url": url, "markdown": f"![{description}]({url})",
            "prompt": img_prompt, "path": str(img_file)}
