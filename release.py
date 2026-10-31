"""Build Keeper.exe and publish it as a GitHub release.

Usage (from the repo folder):
    conda run --no-capture-output -n keeper python release.py            # build + publish
    conda run --no-capture-output -n keeper python release.py --build    # build only

The exe is not committed to git (every 20 MB build would stay in the repo's
history for good); releases hold it instead. The version comes from VERSION in
keeper.py: bump it, commit, then run this. Publishing needs the GitHub CLI
(gh), signed in to an account that can push to the repo.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "Keeper.exe")


def run(*cmd, capture=False):
    r = subprocess.run(cmd, cwd=HERE, text=True, capture_output=capture)
    if r.returncode:
        sys.exit(f"Failed: {' '.join(cmd)}\n{(r.stderr or '').strip()}")
    return (r.stdout or "").strip()


def version():
    with open(os.path.join(HERE, "keeper.py"), encoding="utf-8") as f:
        m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("VERSION not found in keeper.py")
    return m.group(1)


def build():
    run(sys.executable, "-m", "PyInstaller", "keeper.spec", "--noconfirm", "--clean")
    print(f"Built {EXE} ({os.path.getsize(EXE) / 1e6:.1f} MB)")


def main():
    if "--build" in sys.argv:
        return build()
    tag = "v" + version()
    # Release only committed, pushed code, so the tag matches what's built.
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("Commit or stash your changes first.")
    run("git", "fetch", "--quiet", "origin")
    if run("git", "rev-list", "--count", "@{u}..HEAD", capture=True) != "0":
        sys.exit("Push your commits first.")
    if run("git", "tag", "--list", tag, capture=True):
        sys.exit(f"{tag} already exists. Bump VERSION in keeper.py.")
    build()
    run("git", "tag", "-a", tag, "-m", f"Keeper {tag}")
    run("git", "push", "--quiet", "origin", tag)
    run("gh", "release", "create", tag, EXE, "--title", f"Keeper {tag}", "--generate-notes")
    print(f"Published {tag}: " + run("gh", "release", "view", tag, "--json", "url", "-q", ".url", capture=True))


if __name__ == "__main__":
    main()
