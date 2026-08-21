"""What Waikiki may run on the user's behalf, and how it reports back.

Split from :mod:`waikiki.capabilities` so the two questions stay apart: that
module decides *what is true and which step is actionable*; this one owns *what
may be executed at all*. Everything runnable lives in the three registries below
and nowhere else — a caller hands over an id, and nothing a user, a page or an
agent supplies ever reaches an argv.

Two things are load-bearing.

**Installing changes the user's machine.** So a remedy re-checks its own
prerequisite at execution time (the button may have been rendered an hour ago),
runs through :mod:`waikiki.clirun` so the invocation lands in the CLI debug log
like every other spawn here, and reports failure *as* failure: the exit code, a
plain-language cause for the common ones, and what the tool actually said. Exit
0 is treated as a claim rather than as evidence — the tool has to be there
afterwards for the remedy to have worked.

**A script piped into a shell is not a package install.** ``curl … | bash`` runs
whatever the host sends back, with nothing verifying it — a long way from
:mod:`waikiki.updater`, which refuses to execute a release it has not
Ed25519-verified against a build-time-pinned key (CLAUDE.md rule 8). The button
still exists, because a command to copy is no safer and much less usable. What
makes it defensible is the rest: the URL is a constant in this file, per
platform, and the argv is built *from* it; the host is named to the user before
anything happens; and it runs only from a deliberate click plus a confirmation,
never from a probe, a page render or any other side effect.

See ``docs/capabilities.md``.
"""
from __future__ import annotations

import subprocess
import sys

from . import clirun

# Long enough for a global npm install on a slow line, short enough that a wedged
# installer eventually reports failure instead of holding the request forever.
INSTALL_TIMEOUT = 420


# --- what a remedy is allowed to run -----------------------------------------
#
# Everything runnable lives in these three tables and nowhere else. A route
# hands over an id; nothing a caller supplies ever reaches an argv.

# Package-manager installs. `needs` must already be present for the remedy to be
# offered or to run; `provides` must exist afterwards for it to count as worked.
PACKAGE_INSTALLS: dict[str, dict] = {
    "install-claude-cli": {
        "label": "Install the Claude Code CLI",
        "verb": "install the Claude Code CLI",
        "needs": "npm", "provides": "claude",
        "argv": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        "source": "the npm registry",
        "detail": "Installs Anthropic's Claude Code CLI globally from the npm "
                  "registry. This changes your machine and usually takes about "
                  "a minute.",
    },
    "install-gemini-cli": {
        "label": "Install the Gemini CLI",
        "verb": "install the Gemini CLI",
        "needs": "npm", "provides": "gemini",
        "argv": ["npm", "install", "-g", "@google/gemini-cli"],
        "source": "the npm registry",
        "detail": "Installs Google's Gemini CLI globally from the npm registry. "
                  "This changes your machine and usually takes about a minute.",
    },
    "install-node": {
        "label": "Install Node.js first",
        "verb": "install Node.js",
        "needs": "brew", "provides": "npm",
        "argv": ["brew", "install", "node"],
        "source": "Homebrew",
        "detail": "Installs Node.js — which brings npm — using Homebrew. The CLI "
                  "you actually want is installed with npm, and this machine "
                  "hasn't got npm yet, so this comes first.",
    },
    "install-cloudflared": {
        "label": "Install cloudflared",
        "verb": "install cloudflared",
        "needs": "brew", "provides": "cloudflared",
        "argv": ["brew", "install", "cloudflared"],
        "source": "Homebrew",
        "detail": "Installs Cloudflare's tunnel client using Homebrew. It is what "
                  "creates the temporary public address.",
    },
}

# Vendor-published install scripts, downloaded and executed. Riskier than the
# table above by a wide margin (see the module docstring), so: the URL is a
# constant here and is the *only* source of the URL — the argv is built from it,
# so there is one place to read and one place to change; the host is named to
# the user before anything runs; and nothing here is ever triggered by a probe.
#
# Per platform because the vendor publishes a different script per platform.
# Waikiki ships macOS today; the others are declared so adding them later is a
# packaging question rather than a rewrite.
SCRIPT_INSTALLS: dict[str, dict] = {
    "install-agy-cli": {
        "label": "Install the Antigravity CLI",
        "verb": "download and run the Antigravity installer",
        "provides": "agy",
        "vendor": "Google",
        "host": "antigravity.google",
        "platforms": {
            "darwin": {"url": "https://antigravity.google/cli/install.sh",
                       "needs": "curl",
                       "runner": ["/bin/bash", "-c"],
                       "pipeline": "curl -fsSL {url} | bash"},
            "linux": {"url": "https://antigravity.google/cli/install.sh",
                      "needs": "curl",
                      "runner": ["/bin/bash", "-c"],
                      "pipeline": "curl -fsSL {url} | bash"},
            "win32": {"url": "https://antigravity.google/cli/install.ps1",
                      "needs": "powershell",
                      "runner": ["powershell", "-NoProfile", "-Command"],
                      "pipeline": "irm {url} | iex"},
        },
    },
}

# Remedies that open something for the user to act on, rather than changing the
# machine themselves. macOS decides permissions; we can only take you there.
OPEN_REMEDIES: dict[str, dict] = {
    "open-accessibility": {
        "label": "Open Accessibility settings",
        "verb": "open System Settings",
        "detail": "Opens System Settings › Privacy & Security › Accessibility. "
                  "Switch Waikiki on there — macOS asks you, not us, and Waikiki "
                  "cannot grant itself the permission.",
        "urls": {"darwin": "x-apple.systempreferences:com.apple.preference."
                           "security?Privacy_Accessibility"},
        "openers": {"darwin": ["/usr/bin/open"]},
    },
}

def describe(remedy_id: str) -> dict | None:
    """The remedy `remedy_id` as this machine would carry it out, or None.

    None means "not something we can do here" — an unknown id, or a platform the
    vendor publishes nothing for. Callers treat both the same way: no button.
    """
    spec = PACKAGE_INSTALLS.get(remedy_id)
    if spec:
        return {"id": remedy_id, "kind": "install", "label": spec["label"],
                "verb": spec["verb"], "detail": spec["detail"],
                "needs": spec["needs"], "provides": spec["provides"],
                "argv": list(spec["argv"]), "runs": " ".join(spec["argv"]),
                "source": spec["source"], "host": ""}

    spec = SCRIPT_INSTALLS.get(remedy_id)
    if spec:
        plat = spec["platforms"].get(sys.platform)
        if not plat:
            return None
        command = plat["pipeline"].format(url=plat["url"])
        host = spec["host"]
        return {"id": remedy_id, "kind": "script", "label": spec["label"],
                "verb": spec["verb"], "needs": plat["needs"],
                "provides": spec["provides"],
                "argv": list(plat["runner"]) + [command], "runs": command,
                "source": host, "host": host, "vendor": spec["vendor"],
                "url": plat["url"],
                "detail": (
                    f"This downloads an installer script from {host} and runs it "
                    f"on this machine. The script is published by {spec['vendor']}; "
                    f"Waikiki cannot check what it contains first, the way it "
                    f"verifies its own updates before installing them. Continue "
                    f"only if you trust {host}.")}

    spec = OPEN_REMEDIES.get(remedy_id)
    if spec:
        url = spec["urls"].get(sys.platform)
        opener = spec["openers"].get(sys.platform)
        if not url or not opener:
            return None
        return {"id": remedy_id, "kind": "open", "label": spec["label"],
                "verb": spec["verb"], "detail": spec["detail"],
                "needs": "", "provides": "", "argv": list(opener) + [url],
                "runs": url, "source": "System Settings", "host": ""}
    return None


def manual(label: str, why: str, url: str = "", link: str = "",
           settings: str = "") -> dict:
    """A remedy Waikiki cannot perform. Says so, and says where you can.

    Rendered as a greyed-out button beside the instructions, so a row keeps its
    shape whether or not its fix can be pressed — "there is no button for this
    one" is itself the honest answer, and dropping the row would not be.
    """
    return {"id": "", "kind": "manual", "label": label, "why": why,
            "url": url, "link": link, "settings": settings}


# --- carrying a remedy out ---------------------------------------------------

def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _explain(plan: dict, proc) -> str:
    """Why it failed, in words, without hiding that it failed.

    A global install refused for permissions is the single most common outcome
    on a managed Mac, and "npm exited 243" tells the user nothing they can act
    on. Everything unrecognised still reports the exit code rather than being
    smoothed over.
    """
    label = plan["label"]
    blob = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
    provides = plan.get("provides")
    if proc.returncode == 0:
        return (f"{label} reported success, but `{provides}` still isn't "
                f"anywhere Waikiki can run it. It may have installed somewhere "
                f"that isn't on your PATH — quit and reopen Waikiki, then check "
                f"here again.")
    if any(s in blob for s in ("eacces", "eperm", "permission denied",
                               "operation not permitted", "not writable")):
        return (f"{label} failed: this account doesn't have permission to write "
                f"where the install goes. That is the usual outcome of a global "
                f"install on a managed Mac. Waikiki won't ask for your password "
                f"to work around it — install it yourself from a terminal, or point "
                f"Waikiki at a tool you can install.")
    if any(s in blob for s in ("enotfound", "getaddrinfo", "could not resolve",
                               "etimedout", "econnrefused", "network is unreachable",
                               "certificate", "ssl", "proxy", "curl: (")):
        return (f"{label} failed: this machine couldn't reach "
                f"{plan.get('source') or 'the download'}. Check the network (or a "
                f"proxy in the way) and try again.")
    if "enospc" in blob or "no space left" in blob:
        return f"{label} failed: this disk is full."
    return f"{label} failed (exit {proc.returncode})."


def apply(remedy_id: str, which) -> dict:
    """Carry out one remedy. Blocking — call it from a worker thread.

    Never raises: a remedy that dies has to come back as a *reported* failure,
    because the whole point of the button is that the user isn't left reading
    tea leaves. Returns ``{"ok": True, "message"}`` or ``{"ok": False, "error",
    "detail"}``.

    The prerequisite is re-checked here, not just when the button was rendered:
    the page may have been open for an hour, and this is the side that actually
    runs something.
    """
    plan = describe(remedy_id)
    if not plan or plan["kind"] == "manual":
        return {"ok": False, "error": "That isn't something Waikiki can fix here.",
                "detail": ""}
    needs = plan.get("needs")
    if needs and not which(needs):
        return {"ok": False,
                "error": (f"`{needs}` isn't on this machine, so {plan['label']} "
                          f"can't run. Reload this page for the step that is "
                          f"actionable now."),
                "detail": ""}
    try:
        proc = clirun.run(f"remedy:{remedy_id}", plan["argv"], INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": (f"{plan['label']} was still running after "
                          f"{INSTALL_TIMEOUT}s and was stopped. Nothing was "
                          f"rolled back — check the machine before retrying."),
                "detail": ""}
    except Exception as exc:
        return {"ok": False,
                "error": f"{plan['label']} couldn't start: {exc}", "detail": ""}
    provides = plan.get("provides")
    if proc.returncode == 0 and (not provides or which(provides)):
        return {"ok": True, "message": f"{plan['label']} — done.", "detail": ""}
    return {"ok": False, "error": _explain(plan, proc),
            "detail": _tail((proc.stderr or "") or (proc.stdout or ""))}
